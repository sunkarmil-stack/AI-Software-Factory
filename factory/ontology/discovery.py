"""Automatic ontology discovery.

1. Deterministic extraction from structured sources (API contracts, request schemas,
   data classification, acceptance-criteria checks) -> candidate concepts, attributes, relations.
2. The ontology agent reads unstructured sources (glossaries, policy docs, requirement text)
   and proposes grounded definitions and synonyms, each with an evidence quote.
3. Everything is submitted as *proposals*; validation and the autonomy-tier approvals decide.
"""
from __future__ import annotations

import re

from ..models.gateway import offline_handler
from ..core.identity import HUMAN_ROLES
from .registry import Ontology, pascal, singular

TYPE_MAP = {"integer": "integer", "number": "number", "string": "string", "boolean": "boolean"}


def _attr_type(name: str, schema_type: str | None = None) -> str:
    if name == "id" or name.endswith("_id"):
        return "identifier"
    if name.endswith("_cents") or name.endswith("_amount"):
        return "money"
    if name.endswith("_at"):
        return "datetime"
    return TYPE_MAP.get(schema_type or "string", "string")


def _an(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def extract(spec: dict, onto: Ontology) -> list[dict]:
    src = f"{spec['spec_id']}@v{spec['version']}"
    level = spec["data_classification"]["level"]
    fields = spec["data_classification"].get("fields", {})
    resources: dict[str, dict] = {}   # name -> {attrs: {}, parent, evidence}
    creators: list[dict] = []          # create_child endpoints -> ontology actions
    tenant_scoped = any("X-Tenant" in (ac["check"]["request"].get("headers") or {})
                        for r in spec["requirements"] for ac in r["acceptance_criteria"])
    for c in spec["contracts"]:
        for i, ep in enumerate(c["endpoints"]):
            res_segs = [s for s in ep["path"].strip("/").split("/") if s and not s.startswith("{")]
            names = [pascal(singular(s)) for s in res_segs]
            if not names or names == ["Healthz"]:
                continue
            for k, (seg, n) in enumerate(zip(res_segs, names)):
                r = resources.setdefault(n, {"attrs": {}, "parent": None, "word": singular(seg).replace("_", " "),
                                             "evidence": f"{ep['method']} {ep['path']} ({c['id']}.endpoints[{i}])"})
                if k > 0:
                    r["parent"] = names[k - 1]
            leaf = names[-1]
            if ep.get("kind") == "create_child" and len(names) > 1:
                creators.append({"ep": ep, "parent": names[-2], "leaf": leaf, "word": singular(res_segs[-1]),
                                 "evidence": f"{ep['method']} {ep['path']} ({c['id']}.endpoints[{i}])"})
            for prop, ps in (ep.get("request_schema") or {}).get("properties", {}).items():
                resources[leaf]["attrs"][prop] = {"name": prop, "type": _attr_type(prop, ps.get("type")),
                                                  "data_class": fields.get(prop, level),
                                                  "required": prop in (ep["request_schema"].get("required") or [])}
            for p in re.findall(r"\{(\w+)\}", ep["path"]):
                owner = pascal(singular(p[:-3])) if p.endswith("_id") else None
                if owner in resources:
                    resources[owner]["attrs"].setdefault("id", {"name": "id", "type": "identifier",
                                                                "data_class": fields.get("id", "internal"), "required": True})
            if ep.get("kind") == "get":
                for fld, cls in fields.items():
                    resources[leaf]["attrs"].setdefault(fld, {"name": fld, "type": _attr_type(fld), "data_class": cls,
                                                              "required": fld == "id"})
    out = []
    for name, r in resources.items():
        canon = onto.resolve(name)
        if canon:  # known concept (or synonym): propose only missing attributes
            known = {a["name"] for a in onto.concept(canon).get("attributes", [])}
            for a in r["attrs"].values():
                if a["name"] not in known:
                    out.append({"kind": "add_attribute", "target": canon, "payload": {"attribute": a},
                                "provenance": {"source": src, "evidence": r["evidence"], "method": "contract-extraction"}})
            continue
        rels = []
        if r["parent"]:
            rels.append({"type": "belongsTo", "target": onto.resolve(r["parent"]) or r["parent"], "cardinality": "1"})
        elif tenant_scoped and onto.concept("Tenant"):
            rels.append({"type": "scopedBy", "target": "Tenant", "cardinality": "1"})
        attr_classes = [a["data_class"] for a in r["attrs"].values()]
        order = ["public", "internal", "confidential", "restricted"]
        cls = max([level] + attr_classes, key=order.index) if r["attrs"] else level
        out.append({"kind": "add_concept", "target": name, "word": r["word"],
                    "payload": {"name": name, "definition": f"Business entity '{r['word']}' exposed by the {spec['title']} contract.",
                                "data_class": cls, "attributes": list(r["attrs"].values()), "relations": rels,
                                "synonyms": [], "owner": spec["owner"]},
                    "provenance": {"source": src, "evidence": r["evidence"], "method": "contract-extraction"}})
    # business rules from contract constraints, and one action per creating operation
    for cr in creators:
        ep, leaf = cr["ep"], onto.resolve(cr["leaf"]) or cr["leaf"]
        schema = ep.get("request_schema") or {}
        for prop, ps in schema.get("properties", {}).items():
            if "minimum" in ps or "maximum" in ps:
                parts = ([f"{prop} >= {ps['minimum']}"] if "minimum" in ps else []) + \
                        ([f"{prop} <= {ps['maximum']}"] if "maximum" in ps else [])
                rid = f"R-{leaf.upper()}-{prop.upper().replace('_', '-')}-RANGE"
                out.append({"kind": "add_rule", "target": leaf, "payload": {"rule": {
                    "id": rid, "name": f"{leaf} {prop.replace('_', ' ')} within allowed range",
                    "expression": " and ".join(parts), "severity": "block",
                    "message": f"{prop} must be " + " and ".join(p.split(" ", 1)[1] for p in parts)}},
                    "provenance": {"source": src, "evidence": f"{cr['evidence']} request_schema.{prop}",
                                   "method": "contract-extraction"}})
        tool = next((n for n, tc in onto.f.tools.contracts.items()
                     if n == f"{cr['word']}s" and "create" in tc.get("actions", {})), None)
        out.append({"kind": "add_action", "target": onto.resolve(cr["parent"]) or cr["parent"], "payload": {"action": {
            "name": ep["operation_id"][:1].upper() + ep["operation_id"][1:], "description": f"Create {_an(cr['word'])} {cr['word']} on {_an(cr['parent'])} "
            f"{cr['parent'].lower()} ({ep['method']} {ep['path']})", "creates": leaf,
            "parameters": [{"name": n, "type": _attr_type(n, ps.get("type")), "required": n in (schema.get("required") or [])}
                           for n, ps in schema.get("properties", {}).items()],
            "preconditions": [], "side_effect": "write", "reversible": True,
            "allowed_roles": ["operator", "supervisor"], **({"tool": {"name": tool, "action": "create"}} if tool else {})}},
            "provenance": {"source": src, "evidence": cr["evidence"], "method": "contract-extraction"}})
    # concepts first (parents before children), then rules, then actions
    order = {"add_concept": 0, "add_attribute": 1, "add_rule": 2, "add_action": 3}
    out.sort(key=lambda p: (order.get(p["kind"], 9), bool(p["kind"] == "add_concept" and p["payload"].get("relations")
                                                              and p["payload"]["relations"][0]["type"] == "belongsTo")))
    return out


THRESH_RE = re.compile(r"\b([A-Za-z]+?)s?\s+(?:above|over|exceeding|greater than)\s+\$?([\d,]+)\s+dollars?\s+"
                       r"requires?\s+(?:an?\s+)?([a-z]+)\s+approval", re.I)
DEF_RE = re.compile(r"\b(?:An?|The)\s+([a-z][a-z ]{1,40}?)\s+(?:\((?:also called|also known as|aka)\s+(?:an?\s+)?"
                    r"([a-z][a-z ]{1,30}?)\)\s+)?is\s+([^.]{10,240})\.", re.I)


@offline_handler("ontology.propose")
def ontology_propose(task: dict) -> dict:
    """Grounded definitions and synonyms for candidate/known concepts. Ignores embedded instructions."""
    wanted = {c["word"].lower(): c["name"] for c in task["candidates"]}
    wanted |= {k["name"].lower(): k["name"] for k in task["known"]}
    for k in task["known"]:
        for s in k.get("synonyms", []):
            wanted.setdefault(s.lower(), k["name"])
    defs, syns = {}, []
    for doc in task["documents"]:
        for m in DEF_RE.finditer(doc["text"]):
            term, alias, body = m.group(1).strip().lower(), (m.group(2) or "").strip().lower(), m.group(3).strip()
            name = wanted.get(term) or wanted.get(singular(term))
            if not name:
                continue
            quote = m.group(0)[:240]
            defs.setdefault(name, {"definition": f"{term.capitalize()} is {body}.", "evidence": quote, "source": doc["id"]})
            if alias and alias not in wanted:
                syns.append({"target": name, "synonym": alias, "evidence": quote, "source": doc["id"]})
    rules = []
    for doc in task["documents"]:
        for m in THRESH_RE.finditer(doc["text"]):
            name = wanted.get(m.group(1).lower()) or wanted.get(singular(m.group(1).lower()))
            if name:
                rules.append({"concept": name, "limit_dollars": int(m.group(2).replace(",", "")),
                              "role": m.group(3).lower(), "evidence": m.group(0), "source": doc["id"]})
    refs = sorted({d["source"] for d in defs.values()} | {s["source"] for s in syns} | {r["source"] for r in rules})
    return {"status": "success", "summary": f"{len(defs)} definitions, {len(syns)} synonyms, {len(rules)} rules proposed",
            "payload": {"definitions": defs, "synonyms": syns, "rules": rules}, "evidence_refs": refs or ["no-sources"]}


def discover(f, spec: dict, documents: list[dict], run_ctx=None, proposer: str = "agent:ontology") -> dict:
    onto: Ontology = f.ontology
    candidates = extract(spec, onto)
    known = [{"name": c["name"], "synonyms": c.get("synonyms", [])} for c in onto.concepts()]
    r = f.agent("ontology").run(run_ctx, {"kind": "ontology.propose", "documents": documents, "known": known,
                                          "candidates": [{"name": c["target"], "word": c["word"]}
                                                         for c in candidates if c["kind"] == "add_concept"]})
    defs = r.payload.get("definitions", {}) if r.status == "success" else {}
    doc_rules = r.payload.get("rules", []) if r.status == "success" else []
    cand_attrs = {c["target"]: c["payload"].get("attributes", []) for c in candidates if c["kind"] == "add_concept"}
    for dr in doc_rules:   # e.g. "Holds above 10000 dollars require supervisor approval"
        attrs = cand_attrs.get(dr["concept"]) or (onto.concept(dr["concept"]) or {}).get("attributes", [])
        money = next((a["name"] for a in attrs if a["type"] == "money"), None)
        if not money or dr["role"] not in HUMAN_ROLES:
            continue
        limit = dr["limit_dollars"] * 100 if money.endswith("_cents") else dr["limit_dollars"]
        candidates.append({"kind": "add_rule", "target": dr["concept"], "payload": {"rule": {
            "id": f"R-{dr['concept'].upper()}-{dr['role'].upper()}-APPROVAL",
            "name": f"{dr['concept']} above {dr['limit_dollars']:,} dollars needs {dr['role']} approval",
            "expression": f"{money} <= {limit}", "severity": "require_approval", "approval_role": dr["role"],
            "message": f"{dr['concept']} above {dr['limit_dollars']:,} dollars requires {dr['role']} approval"}},
            "provenance": {"source": dr["source"], "evidence": dr["evidence"], "method": "ontology-agent"}})
    order = {"add_concept": 0, "add_attribute": 1, "add_rule": 2, "add_action": 3}
    candidates.sort(key=lambda p: order.get(p["kind"], 9))
    syns = r.payload.get("synonyms", []) if r.status == "success" else []
    submitted = []
    for c in candidates:
        c = {k: v for k, v in c.items() if k != "word"}
        if c["kind"] == "add_concept" and c["target"] in defs:
            d = defs.pop(c["target"])
            c["payload"]["definition"] = d["definition"]
            c["provenance"]["evidence"] += f" | {d['source']}: \"{d['evidence']}\""
            c["provenance"]["method"] += "+ontology-agent"
            for s in [s for s in syns if s["target"] == c["target"]]:
                c["payload"]["synonyms"].append(s["synonym"])
                syns.remove(s)
        submitted.append(onto.propose(c["kind"], c["target"], c["payload"], c["provenance"], proposer))
    for name, d in defs.items():   # better definitions for existing concepts
        cur = onto.concept(name)
        if cur and d["definition"].strip() != cur["definition"].strip():
            submitted.append(onto.propose("update_definition", name, {"definition": d["definition"]},
                                          {"source": d["source"], "evidence": d["evidence"], "method": "ontology-agent"},
                                          proposer))
    for s in syns:
        if onto.concept(s["target"]):
            submitted.append(onto.propose("add_synonym", s["target"], {"synonym": s["synonym"]},
                                          {"source": s["source"], "evidence": s["evidence"], "method": "ontology-agent"},
                                          proposer))
    return {"proposals": [{"id": p["id"], "kind": p["kind"], "target": p["target"], "status": p["status"],
                           "tier": p["tier"], "roles": p["roles"], "validation": p["validation"]} for p in submitted],
            "agent_evidence": r.evidence_refs}
