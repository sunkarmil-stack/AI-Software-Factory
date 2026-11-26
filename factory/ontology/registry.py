"""Governed, versioned business ontology.

Agents and extractors may *propose* ontology changes automatically; only validated and
approved proposals change the ontology. Approval requirements follow the factory autonomy
model (tier by change kind and data class). Concepts are trace-graph nodes, so a breaking
concept change marks dependent specs, releases and approvals stale, exactly like a spec change.

Concept document:
  name: Account                      # PascalCase, unique
  definition: str
  data_class: public|internal|confidential|restricted
  attributes: [{name, type, data_class, required}]
  relations:  [{type, target, cardinality}]     # cardinality: 1, 0..1, 0..*, 1..*
  synonyms:   [str]
  standard_ref: optional IRI (e.g. FIBO), verify against the adopted release
  owner: str
"""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import yaml

from ..core.approvals import ApprovalError, ApprovalService
from ..core.db import DB, j, unj
from ..core.evidence import Ledger
from ..core.identity import HUMAN_ROLES
from ..core.util import digest_obj, new_id, now
from . import rules as rule_engine

SCHEMA = """
CREATE TABLE IF NOT EXISTS onto_concepts(name TEXT, version INTEGER, body TEXT, digest TEXT, semantic_digest TEXT,
  status TEXT, created REAL, created_by TEXT, proposal TEXT, PRIMARY KEY(name, version));
CREATE TABLE IF NOT EXISTS onto_proposals(id TEXT PRIMARY KEY, kind TEXT, target TEXT, payload TEXT, payload_digest TEXT,
  provenance TEXT, proposer TEXT, proposer_kind TEXT, tier INTEGER, roles TEXT, status TEXT, validation TEXT,
  approvals TEXT, decided_by TEXT, reason TEXT, created REAL, decided REAL, target_version INTEGER DEFAULT 0);
"""
KINDS = {"add_concept", "add_attribute", "add_synonym", "update_definition", "add_relation", "deprecate_concept",
         "add_rule", "update_rule", "retire_rule", "add_action", "retire_action"}
RULE_KINDS = {"add_rule", "update_rule", "retire_rule"}
ACTION_KINDS = {"add_action", "retire_action"}
SEVERITIES = {"block", "warn", "require_approval"}
RULE_ID_RE = re.compile(r"^R-[A-Z0-9][A-Z0-9-]*$")
DATA_CLASSES = ["public", "internal", "confidential", "restricted"]
CARDINALITIES = {"1", "0..1", "0..*", "1..*"}
TYPES = {"string", "integer", "number", "boolean", "date", "datetime", "money", "identifier", "enum"}
NAME_RE = re.compile(r"^[A-Z][A-Za-z0-9]+$")
ATTR_RE = re.compile(r"^[a-z][a-z0-9_]*$")
REL_RE = re.compile(r"^[a-z][A-Za-z0-9]+$")
# rule changes alter behaviour that implementations must enforce, so they invalidate dependents;
# adding an action only adds capability
BREAKING = {"update_definition", "deprecate_concept", "add_rule", "update_rule", "retire_rule", "retire_action"}


class OntologyError(ValueError):
    pass


def semantic(body: dict) -> dict:
    """Fields that carry meaning; labels/synonyms excluded so they never invalidate dependents."""
    return {k: body.get(k) for k in ("definition", "data_class", "attributes", "relations", "status", "rules", "actions")}


def singular(word: str) -> str:
    w = word.lower()
    if w.endswith("ies"):
        return w[:-3] + "y"
    if w.endswith("sses") or w.endswith("shes") or w.endswith("ches"):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def pascal(term: str) -> str:
    return "".join(p.capitalize() for p in re.split(r"[\s_\-]+", term.strip()) if p)


class Ontology:
    def __init__(self, f):
        self.f = f
        self.db: DB = f.db
        self.ledger: Ledger = f.ledger

    # ------------------------------------------------------------ reads
    def concept(self, name: str, version: int | None = None) -> dict | None:
        if version is None:
            r = self.db.one("SELECT * FROM onto_concepts WHERE name=? ORDER BY version DESC LIMIT 1", (name,))
        else:
            r = self.db.one("SELECT * FROM onto_concepts WHERE name=? AND version=?", (name, version))
        if not r:
            return None
        return {**unj(r["body"]), "version": r["version"], "status": r["status"], "digest": r["digest"],
                "created": r["created"], "created_by": r["created_by"], "proposal": r["proposal"]}

    def concepts(self, include_deprecated: bool = False) -> list[dict]:
        out = []
        for r in self.db.all("SELECT name, MAX(version) v FROM onto_concepts GROUP BY name ORDER BY name"):
            c = self.concept(r["name"], r["v"])
            if include_deprecated or c["status"] == "active":
                out.append(c)
        return out

    def history(self, name: str) -> list[dict]:
        return [{"version": r["version"], "status": r["status"], "digest": r["digest"], "created": r["created"],
                 "created_by": r["created_by"], "proposal": r["proposal"]}
                for r in self.db.all("SELECT * FROM onto_concepts WHERE name=? ORDER BY version", (name,))]

    def resolve(self, term: str) -> str | None:
        """Canonical concept name for a term, label or synonym (case-insensitive, singular/plural)."""
        t = term.strip().lower().replace("_", " ")
        cands = {t, singular(t), pascal(t).lower(), pascal(singular(t)).lower()}
        for c in self.concepts():
            if c["name"].lower() in cands or any(x.lower() in cands for x in c.get("synonyms", [])):
                return c["name"]
        return None

    def proposal(self, pid: str) -> dict | None:
        r = self.db.one("SELECT * FROM onto_proposals WHERE id=?", (pid,))
        if not r:
            return None
        for k in ("payload", "provenance", "roles", "validation", "approvals"):
            r[k] = unj(r[k])
        return r

    def proposals(self, status: str | None = None) -> list[dict]:
        rows = self.db.all("SELECT id FROM onto_proposals" + (" WHERE status=?" if status else "") + " ORDER BY created",
                           (status,) if status else ())
        return [self.proposal(r["id"]) for r in rows]

    # ------------------------------------------------------------ tiering (autonomy model)
    def classify(self, kind: str, payload: dict, target: dict | None) -> tuple[int, list[str]]:
        if kind == "add_synonym":
            tier, roles = 1, []
        elif kind == "add_attribute":
            tier, roles = 2, ["data_owner"]
        elif kind in RULE_KINDS:   # business rules are owned by the business
            tier, roles = 3, ["ontology_owner", "product_owner"]
        elif kind in ACTION_KINDS:
            tier, roles = 3, ["ontology_owner", "architect"]
            a = payload.get("action") or {}
            if a.get("side_effect") == "consequential" or a.get("reversible") is False:
                roles = roles + ["security"]
        else:
            tier, roles = 3, ["ontology_owner", "architect"]
        classes = [payload.get("data_class"), (target or {}).get("data_class")]
        classes += [a.get("data_class") for a in payload.get("attributes", [])]
        if payload.get("attribute"):
            classes.append(payload["attribute"].get("data_class"))
        if "restricted" in classes:  # restricted data always needs the data owner and security
            tier, roles = 3, sorted(set(roles) | {"data_owner", "security"})
        return tier, roles

    # ------------------------------------------------------------ validation
    _applying: str | None = None   # proposal being applied: excluded from "already pending" checks

    def _pending_names(self) -> set[str]:
        return {p["payload"]["name"] for p in self.proposals("pending")
                if p["kind"] == "add_concept" and p["id"] != self._applying}

    def validate(self, kind: str, target: str, payload: dict, provenance: dict) -> list[str]:
        errs: list[str] = []
        if kind not in KINDS:
            return [f"unknown proposal kind {kind}"]
        if not provenance or not provenance.get("source") or not provenance.get("evidence"):
            errs.append("provenance with source and evidence quote is required")
        cur = self.concept(target) if target else None
        existing = {c["name"] for c in self.concepts(include_deprecated=True)}
        all_syn = {s.lower(): c["name"] for c in self.concepts() for s in c.get("synonyms", [])}
        if kind == "add_concept":
            name = payload.get("name", "")
            if not NAME_RE.match(name):
                errs.append(f"concept name '{name}' must be PascalCase")
            if name in existing:
                errs.append(f"concept {name} already exists (propose an update instead)")
            if name.lower() in all_syn:
                errs.append(f"{name} is already a synonym of {all_syn[name.lower()]}")
            if name in self._pending_names():
                errs.append(f"a pending proposal already adds {name}")
            if not (payload.get("definition") or "").strip():
                errs.append("definition is required")
            if payload.get("data_class") not in DATA_CLASSES:
                errs.append(f"data_class must be one of {DATA_CLASSES}")
            errs += self._check_attrs(payload.get("attributes", []), payload.get("data_class"))
            for r in payload.get("relations", []):
                errs += self._check_rel(r, existing)
        elif kind in ("add_rule", "add_action") and not cur and target in self._pending_names():
            pass  # target concept awaits approval; applied once it exists
        else:
            if not cur:
                errs.append(f"target concept {target} does not exist")
                return errs
            if cur["status"] != "active":
                errs.append(f"target concept {target} is {cur['status']}")
        if kind == "add_attribute":
            a = payload.get("attribute", {})
            if a.get("name") in {x["name"] for x in cur.get("attributes", [])}:
                errs.append(f"{target}.{a.get('name')} already exists")
            errs += self._check_attrs([a], cur["data_class"])
        if kind == "add_synonym":
            s = (payload.get("synonym") or "").strip()
            if not s:
                errs.append("synonym is empty")
            elif s.lower() in all_syn or pascal(s) in existing:
                owner = all_syn.get(s.lower(), pascal(s))
                if owner != target:
                    errs.append(f"'{s}' already denotes {owner}; ambiguous synonym rejected")
                else:
                    errs.append(f"'{s}' is already a synonym of {target}")
        if kind == "update_definition":
            if not (payload.get("definition") or "").strip():
                errs.append("definition is required")
            elif payload["definition"].strip() == cur.get("definition", "").strip():
                errs.append("definition unchanged")
        if kind == "add_relation":
            errs += self._check_rel(payload.get("relation", {}), existing)
            r = payload.get("relation", {})
            if any(x["type"] == r.get("type") and x["target"] == r.get("target") for x in cur.get("relations", [])):
                errs.append("relation already exists")
        if kind in RULE_KINDS:
            errs += self._check_rule(kind, target, payload, cur)
        if kind in ACTION_KINDS:
            errs += self._check_action(kind, target, payload, cur)
        return errs

    # ---- rules & actions helpers
    def attrs_of(self, name: str) -> set[str] | None:
        c = self.concept(name)
        if c:
            return {a["name"] for a in c.get("attributes", [])}
        for p in self.proposals("pending"):
            if p["kind"] == "add_concept" and p["payload"]["name"] == name:
                return {a["name"] for a in p["payload"].get("attributes", [])}
        return None

    def all_rules(self) -> dict[str, dict]:
        return {r["id"]: {**r, "concept": c["name"]} for c in self.concepts() for r in c.get("rules", [])
                if r.get("status", "active") == "active"}

    def all_actions(self) -> dict[str, dict]:
        return {a["name"]: {**a, "concept": c["name"]} for c in self.concepts() for a in c.get("actions", [])
                if a.get("status", "active") == "active"}

    def _pending_ids(self, kind: str, key: str) -> set[str]:
        return {p["payload"].get(key, {}).get("id" if key == "rule" else "name")
                for p in self.proposals("pending") if p["kind"] == kind and p["id"] != self._applying}

    def _check_rule(self, kind: str, target: str, payload: dict, cur: dict | None) -> list[str]:
        errs = []
        existing = self.all_rules()
        if kind == "retire_rule":
            rid = payload.get("rule_id")
            if rid not in {r["id"] for r in (cur or {}).get("rules", []) if r.get("status", "active") == "active"}:
                return [f"rule {rid} is not an active rule of {target}"]
            users = [a["name"] for a in self.all_actions().values() if rid in a.get("preconditions", [])]
            return [f"rule {rid} is a precondition of actions {users}; retire or change them first"] if users else []
        r = payload.get("rule") or {}
        rid = r.get("id", "")
        if not RULE_ID_RE.match(rid):
            errs.append(f"rule id '{rid}' must look like R-HOLD-AMOUNT")
        if kind == "add_rule" and (rid in existing or rid in self._pending_ids("add_rule", "rule")):
            errs.append(f"rule {rid} already exists")
        if kind == "update_rule":
            old = next((x for x in (cur or {}).get("rules", []) if x["id"] == rid), None)
            if not old:
                errs.append(f"rule {rid} is not a rule of {target}")
            elif {k: old.get(k) for k in ("expression", "severity", "approval_role", "message")} == \
                    {k: r.get(k) for k in ("expression", "severity", "approval_role", "message")}:
                errs.append("rule unchanged")
        if not (r.get("name") or "").strip() or not (r.get("message") or "").strip():
            errs.append("rule name and message are required")
        if r.get("severity") not in SEVERITIES:
            errs.append(f"severity must be one of {sorted(SEVERITIES)}")
        if r.get("severity") == "require_approval" and r.get("approval_role") not in HUMAN_ROLES:
            errs.append("require_approval rules need approval_role set to a human role")
        try:
            used = rule_engine.names(r.get("expression", ""))
            attrs = self.attrs_of(target) or set()
            unknown = used - attrs
            if unknown:
                errs.append(f"expression uses {sorted(unknown)} which are not attributes of {target}")
        except rule_engine.RuleSyntaxError as e:
            errs.append(f"expression rejected: {e}")
        return errs

    def _check_action(self, kind: str, target: str, payload: dict, cur: dict | None) -> list[str]:
        if kind == "retire_action":
            n = payload.get("action_name")
            ok = n in {a["name"] for a in (cur or {}).get("actions", []) if a.get("status", "active") == "active"}
            return [] if ok else [f"action {n} is not an active action of {target}"]
        errs = []
        a = payload.get("action") or {}
        name = a.get("name", "")
        if not NAME_RE.match(name):
            errs.append(f"action name '{name}' must be PascalCase (e.g. PlaceHold)")
        if name in self.all_actions() or name in self._pending_ids("add_action", "action"):
            errs.append(f"action {name} already exists")
        if not (a.get("description") or "").strip():
            errs.append("action description is required")
        creates = a.get("creates")
        allowed_params = self.attrs_of(creates) if creates else None
        if creates and allowed_params is None:
            errs.append(f"action creates {creates}, which is neither an existing nor a pending concept")
        seen = set()
        for prm in a.get("parameters", []):
            n = prm.get("name", "")
            if not ATTR_RE.match(n) or n in seen:
                errs.append(f"parameter '{n}' must be unique snake_case")
            seen.add(n)
            if prm.get("type") not in TYPES:
                errs.append(f"parameter {n}: type must be one of {sorted(TYPES)}")
            if allowed_params is not None and n not in allowed_params:
                errs.append(f"parameter {n} is not an attribute of {creates}")
        unknown_rules = set(a.get("preconditions", [])) - set(self.all_rules())
        if unknown_rules:
            errs.append(f"preconditions reference unknown or inactive rules {sorted(unknown_rules)}")
        if a.get("side_effect") not in ("write", "consequential"):
            errs.append("side_effect must be write or consequential")
        if not isinstance(a.get("reversible"), bool):
            errs.append("reversible must be true or false")
        bad_roles = set(a.get("allowed_roles", [])) - HUMAN_ROLES
        if bad_roles:
            errs.append(f"allowed_roles contains unknown roles {sorted(bad_roles)}")
        tool = a.get("tool")
        if tool:
            c = self.f.tools.contracts.get(tool.get("name"))
            if not c or tool.get("action") not in c.get("actions", {}):
                errs.append(f"tool binding {tool} is not a registered tool contract action")
            else:
                ts = c["actions"][tool["action"]]["side_effect"]
                if ts == "consequential" and a.get("side_effect") != "consequential":
                    errs.append("action must declare side_effect consequential: its bound tool is consequential")
        return errs

    def _check_attrs(self, attrs: list[dict], concept_class: str | None) -> list[str]:
        errs, seen = [], set()
        for a in attrs:
            n = a.get("name", "")
            if not ATTR_RE.match(n):
                errs.append(f"attribute '{n}' must be snake_case")
            if n in seen:
                errs.append(f"duplicate attribute {n}")
            seen.add(n)
            if a.get("type") not in TYPES:
                errs.append(f"attribute {n}: type must be one of {sorted(TYPES)}")
            if a.get("data_class") not in DATA_CLASSES:
                errs.append(f"attribute {n}: data_class required")
            elif concept_class in DATA_CLASSES and DATA_CLASSES.index(a["data_class"]) > DATA_CLASSES.index(concept_class):
                errs.append(f"attribute {n} ({a['data_class']}) is more sensitive than its concept ({concept_class}); "
                            f"raise the concept data class first")
        return errs

    def _check_rel(self, r: dict, existing: set[str]) -> list[str]:
        errs = []
        if not REL_RE.match(r.get("type", "")):
            errs.append(f"relation type '{r.get('type')}' must be camelCase")
        if r.get("cardinality") not in CARDINALITIES:
            errs.append(f"relation cardinality must be one of {sorted(CARDINALITIES)}")
        if r.get("target") not in existing and r.get("target") not in self._pending_names():
            errs.append(f"relation target {r.get('target')} is neither an existing nor a pending concept")
        return errs

    # ------------------------------------------------------------ proposals
    def propose(self, kind: str, target: str | None, payload: dict, provenance: dict, proposer: str) -> dict:
        p = self.f.directory.get(proposer)
        target = target or payload.get("name")
        errs = self.validate(kind, target, payload, provenance)
        cur = self.concept(target) if target else None
        tier, roles = self.classify(kind, payload, cur)
        pid = new_id("ONP")
        status = "invalid" if errs else "pending"
        tv = (cur or {}).get("version", 0) if kind != "add_concept" else 0
        self.db.execute("INSERT INTO onto_proposals(id,kind,target,payload,payload_digest,provenance,proposer,proposer_kind,"
                        "tier,roles,status,validation,approvals,created,target_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (pid, kind, target, j(payload), digest_obj(payload), j(provenance), proposer, p.kind, tier,
                         j(roles), status, j(errs), j([]), now(), tv))
        self.ledger.record("ontology.proposed", pid, proposer, {"kind": kind, "target": target, "tier": tier,
                                                               "roles": roles, "valid": not errs, "errors": errs,
                                                               "source": provenance.get("source")})
        if not errs and tier == 1:
            self._apply(pid, actor="ontology-registry", auto=True)
        return self.proposal(pid)

    def binding(self, pid: str) -> dict:
        p = self.proposal(pid)
        # approvals bind to the target version seen at proposal time
        return ApprovalService.binding("ontology.change", p["payload_digest"], "ontology", f"{p['kind']}:{p['target']}",
                                       {"target_version": p.get("target_version") or 0})

    def approve(self, pid: str, approver: str, role: str, rationale: str = "") -> dict:
        p = self.proposal(pid)
        if not p or p["status"] != "pending":
            raise OntologyError(f"proposal {pid} is not pending")
        if role not in p["roles"]:
            raise OntologyError(f"role {role} is not required for {pid} (requires {p['roles']})")
        if digest_obj(p["payload"]) != p["payload_digest"]:
            raise OntologyError("proposal payload was altered after submission")
        aid = self.f.approvals.approve(approver, role, self.binding(pid), author_ids={p["proposer"]}, rationale=rationale)
        ids = p["approvals"] + [aid]
        self.db.execute("UPDATE onto_proposals SET approvals=? WHERE id=?", (j(ids), pid))
        satisfied = {self.f.approvals.get(a)["role"] for a in ids}
        if set(p["roles"]) <= satisfied:
            try:
                self._apply(pid, actor=approver)
            except OntologyError as e:
                if "cannot apply yet" not in str(e):
                    raise
                self.db.execute("UPDATE onto_proposals SET reason=? WHERE id=?", (str(e), pid))
        return self.proposal(pid)

    def _retry_ready(self) -> None:
        """Apply approved proposals that were waiting for a dependency (e.g. their concept)."""
        for p in self.proposals("pending"):
            got = {self.f.approvals.get(a)["role"] for a in p["approvals"]}
            if p["roles"] and set(p["roles"]) <= got:
                try:
                    self._apply(p["id"], actor="ontology-registry")
                except OntologyError:
                    pass

    def reject(self, pid: str, actor: str, reason: str) -> dict:
        p = self.proposal(pid)
        who = self.f.directory.get(actor)
        if who.kind != "human" or not (set(p["roles"]) & who.roles or who.has_role("ontology_owner")):
            raise OntologyError("only a required approver or the ontology owner can reject")
        self.db.execute("UPDATE onto_proposals SET status='rejected', decided_by=?, reason=?, decided=? WHERE id=?",
                        (actor, reason, now(), pid))
        self.ledger.record("ontology.rejected", pid, actor, {"reason": reason})
        return self.proposal(pid)

    # ------------------------------------------------------------ apply
    def _apply(self, pid: str, actor: str, auto: bool = False) -> None:
        p = self.proposal(pid)
        if p["status"] != "pending":
            return  # already applied (e.g. by a nested retry) or decided
        if digest_obj(p["payload"]) != p["payload_digest"]:
            raise OntologyError("proposal payload was altered after submission")
        if not auto:
            try:
                self.f.approvals.require(p["approvals"], self.binding(pid), p["roles"])
            except ApprovalError as e:
                raise OntologyError(f"approvals invalid: {e}")
        cur_v = (self.concept(p["target"]) or {}).get("version", 0)
        # changes that rewrite existing meaning must be re-reviewed if the concept moved on;
        # additive changes (attributes, synonyms, relations, new rules/actions) rebase safely
        rewrites = {"update_definition", "update_rule", "retire_rule", "retire_action", "deprecate_concept"}
        if p["kind"] in rewrites and p.get("target_version") and cur_v != p["target_version"]:
            msg = f"{p['target']} changed since this proposal (v{p['target_version']} -> v{cur_v}); re-propose"
            self.db.execute("UPDATE onto_proposals SET status='invalid', validation=? WHERE id=?", (j([msg]), pid))
            raise OntologyError(f"proposal no longer valid: {msg}")
        if p["kind"] in ("add_rule", "add_action") and not self.concept(p["target"]):
            raise OntologyError(f"cannot apply yet: approve concept {p['target']} first")
        creates = (p["payload"].get("action") or {}).get("creates")
        if p["kind"] == "add_action" and creates and not self.concept(creates):
            raise OntologyError(f"cannot apply yet: approve concept {creates} first")
        self._applying = pid
        try:
            errs = self.validate(p["kind"], p["target"], p["payload"], p["provenance"])
        finally:
            self._applying = None
        if any("neither an existing" in e for e in errs):
            raise OntologyError(f"cannot apply yet: {errs} — approve the target concept first")
        if errs:
            self.db.execute("UPDATE onto_proposals SET status='invalid', validation=? WHERE id=?", (j(errs), pid))
            raise OntologyError(f"proposal no longer valid: {errs}")
        kind, payload, name = p["kind"], p["payload"], p["target"]
        cur = self.concept(name)
        if kind == "add_concept":
            body = {"name": payload["name"], "definition": payload["definition"], "data_class": payload["data_class"],
                    "attributes": payload.get("attributes", []), "relations": payload.get("relations", []),
                    "synonyms": payload.get("synonyms", []), "standard_ref": payload.get("standard_ref"),
                    "owner": payload.get("owner", "ontology-owner"), "rules": [], "actions": []}
        else:
            body = {k: copy.deepcopy(cur[k]) for k in ("name", "definition", "data_class", "attributes", "relations",
                                                       "synonyms", "standard_ref", "owner", "rules", "actions") if k in cur}
            body.setdefault("rules", []); body.setdefault("actions", [])
            if kind == "add_attribute":
                body["attributes"].append(payload["attribute"])
            elif kind == "add_synonym":
                body["synonyms"].append(payload["synonym"].strip())
            elif kind == "update_definition":
                body["definition"] = payload["definition"].strip()
            elif kind == "add_relation":
                body["relations"].append(payload["relation"])
            elif kind == "add_rule":
                body["rules"].append({**payload["rule"], "status": "active", "source": p["provenance"].get("source")})
            elif kind == "update_rule":
                body["rules"] = [({**payload["rule"], "status": "active", "source": p["provenance"].get("source")}
                                  if r["id"] == payload["rule"]["id"] else r) for r in body["rules"]]
            elif kind == "retire_rule":
                body["rules"] = [({**r, "status": "retired"} if r["id"] == payload["rule_id"] else r) for r in body["rules"]]
            elif kind == "add_action":
                body["actions"].append({**payload["action"], "status": "active", "source": p["provenance"].get("source")})
            elif kind == "retire_action":
                body["actions"] = [({**a, "status": "retired"} if a["name"] == payload["action_name"] else a)
                                   for a in body["actions"]]
        status = "deprecated" if kind == "deprecate_concept" else "active"
        self._write(body, status, actor, pid, breaking=kind in BREAKING)
        self.db.execute("UPDATE onto_proposals SET status=?, decided_by=?, decided=?, reason=NULL WHERE id=?",
                        ("auto_accepted" if auto else "accepted", actor, now(), pid))
        self._retry_ready()

    def _write(self, body: dict, status: str, actor: str, proposal: str | None, breaking: bool) -> dict:
        name = body["name"]
        prev = self.concept(name)
        version = (prev["version"] + 1) if prev else 1
        sem = digest_obj(semantic({**body, "status": status}))
        self.db.execute("INSERT INTO onto_concepts(name,version,body,digest,semantic_digest,status,created,created_by,proposal)"
                        " VALUES(?,?,?,?,?,?,?,?,?)", (name, version, j(body), digest_obj(body), sem, status, now(), actor,
                                                       proposal))
        t = self.f.trace
        old = t.current("CONCEPT", name, "ontology") if prev else None   # read before creating the new node
        new_node = t.node("CONCEPT", name, sem, "ontology", {"version": version})
        stale: list[str] = []
        if prev:
            if old and old["id"] != new_node:
                if breaking:
                    stale = t.supersede(old["id"], f"{name} v{version}")
                    for nid in stale:
                        n = t.get(nid)
                        if n and n["kind"] == "APPROVAL":
                            self.f.approvals.revoke(n["ref"], "ontology-registry", f"stale after {name} v{version}")
                else:  # additive change: move dependents to the new node, nothing becomes stale
                    for e in t.dependents(old["id"]):
                        t.edge(e["src"], new_node, e["rel"])
                    self.db.execute("DELETE FROM trace_edges WHERE dst=?", (old["id"],))   # dependents now point at the new version
                    self.db.execute("UPDATE trace_nodes SET status='superseded' WHERE id=?", (old["id"],))
        self.ledger.record("ontology.applied", f"{name}@v{version}", actor,
                           {"proposal": proposal, "breaking": breaking, "stale": stale, "status": status})
        if not breaking:
            self.link_specs()
        return {"name": name, "version": version, "stale": stale}

    def seed(self, path: Path, actor: str = "admin.pat") -> list[str]:
        """Load the reviewed catalog seed (platform-owned, like the skill catalog)."""
        doc = yaml.safe_load(Path(path).read_text())
        out = []
        for c in doc["concepts"]:
            if self.concept(c["name"]):
                continue
            errs = self._check_attrs(c.get("attributes", []), c["data_class"])
            if errs:
                raise OntologyError(f"seed {c['name']}: {errs}")
            self._write({**c, "attributes": c.get("attributes", []), "relations": c.get("relations", []),
                         "synonyms": c.get("synonyms", []), "rules": c.get("rules", []), "actions": c.get("actions", [])},
                        "active", actor, None, breaking=False)
            out.append(c["name"])
        return out

    # ------------------------------------------------------------ links, impact, checks
    def spec_terms(self, spec: dict) -> dict[str, set[str]]:
        """Concept term -> attribute names a spec uses (from contract paths, schemas and classification)."""
        terms: dict[str, set[str]] = {}
        for c in spec.get("contracts", []):
            for ep in c["endpoints"]:
                segs = [s for s in ep["path"].strip("/").split("/") if s and not s.startswith("{")]
                for s in segs:
                    terms.setdefault(pascal(singular(s)), set())
                if segs and ep.get("request_schema"):
                    terms[pascal(singular(segs[-1]))] |= set(ep["request_schema"].get("properties", {}))
        primary = None
        for c in spec.get("contracts", []):
            for ep in c["endpoints"]:
                if ep.get("kind") == "get":
                    segs = [s for s in ep["path"].strip("/").split("/") if s and not s.startswith("{")]
                    primary = pascal(singular(segs[0])) if segs else None
                    break
        if primary:
            terms.setdefault(primary, set()).update((spec.get("data_classification") or {}).get("fields", {}).keys())
        terms.pop("Healthz", None)
        return terms

    def link_spec(self, spec: dict) -> list[str]:
        t = self.f.trace
        node = t.current("SPEC", f"{spec['spec_id']}@v{spec['version']}", spec.get("project", "unknown"))
        if not node:
            return []
        linked = []
        for term in self.spec_terms(spec):
            name = self.resolve(term) or (term if self.concept(term) else None)
            cn = t.current("CONCEPT", name, "ontology") if name else None
            if cn:
                t.edge(node["id"], cn["id"], "uses_concept")
                linked.append(name)
        return linked

    def link_specs(self) -> None:
        for r in self.db.all("SELECT spec_id, version FROM specs WHERE status='baselined'"):
            self.link_spec(self.f.specs.get(r["spec_id"], r["version"])["body"])

    def impact(self, name: str) -> dict:
        t = self.f.trace
        cn = t.current("CONCEPT", name, "ontology")
        if not cn:
            raise OntologyError(f"concept {name} not found")
        out: dict[str, list[str]] = {}
        frontier, seen = [cn["id"]], {cn["id"]}
        while frontier:
            cur = frontier.pop()
            for e in t.dependents(cur):
                if e["src"] in seen:
                    continue
                seen.add(e["src"])
                n = t.get(e["src"])
                if n and n["status"] in ("current", "stale"):
                    out.setdefault(n["kind"], []).append(n["ref"] + (" (stale)" if n["status"] == "stale" else ""))
                frontier.append(e["src"])
        related = [c["name"] for c in self.concepts() if any(r["target"] == name for r in c.get("relations", []))]
        creators = [a["name"] for a in self.all_actions().values() if a.get("creates") == name]
        return {"concept": name, "dependents": {k: sorted(set(v)) for k, v in out.items()}, "related_concepts": related,
                "created_by_actions": creators}

    def check_spec(self, spec: dict) -> list[dict]:
        """Findings: unknown terms, non-canonical synonyms, unknown attributes, data-class mismatches."""
        findings = []
        for term, attrs in self.spec_terms(spec).items():
            canon = self.resolve(term) or (term if self.concept(term) else None)
            if not canon:
                findings.append({"severity": "warning", "term": term, "finding": "unknown business concept; "
                                 "run ontology discovery or propose it"})
                continue
            if canon != term:
                findings.append({"severity": "info", "term": term, "finding": f"synonym; canonical concept is {canon}"})
            c = self.concept(canon)
            known = {a["name"]: a for a in c.get("attributes", [])}
            for a in sorted(attrs):
                if a not in known:
                    findings.append({"severity": "warning", "term": f"{canon}.{a}", "finding": "attribute not in ontology"})
            for fld, cls in ((spec.get("data_classification") or {}).get("fields") or {}).items():
                if fld in known and canon == pascal(singular(term)) and known[fld]["data_class"] != cls:
                    findings.append({"severity": "error", "term": f"{canon}.{fld}",
                                     "finding": f"spec classifies as {cls} but ontology says {known[fld]['data_class']}"})
        # business rules and actions vs. the API contract
        actions = self.all_actions()
        for c in spec.get("contracts", []):
            for ep in c["endpoints"]:
                if ep.get("kind") != "create_child":
                    continue
                segs = [x for x in ep["path"].strip("/").split("/") if x and not x.startswith("{")]
                leaf = self.resolve(segs[-1]) if segs else None
                if not leaf:
                    continue
                if not any(a.get("creates") == leaf for a in actions.values()):
                    findings.append({"severity": "warning", "term": f"{ep['method']} {ep['path']}",
                                     "finding": f"no governed ontology action creates {leaf}"})
                props = (ep.get("request_schema") or {}).get("properties", {})
                for rule in (self.concept(leaf) or {}).get("rules", []):
                    if rule.get("status", "active") != "active" or rule["severity"] != "block":
                        continue
                    for m in re.finditer(r"(\w+)\s*(>=|<=|>|<)\s*(-?\d+)", rule["expression"]):
                        attr, op, val = m.group(1), m.group(2), int(m.group(3))
                        key = "minimum" if op in (">=", ">") else "maximum"
                        if attr in props and props[attr].get(key) != val:
                            findings.append({"severity": "warning", "term": f"{leaf}.{attr}", "ref": rule["id"],
                                             "finding": f"contract {key} is {props[attr].get(key)} but business rule "
                                                        f"{rule['id']} requires {attr} {op} {val}"})
        for r in spec.get("requirements", []):
            for c in self.concepts():
                for s in c.get("synonyms", []):
                    if re.search(rf"\b{re.escape(s)}\b", r["statement"], re.I):
                        findings.append({"severity": "info", "term": s, "ref": r["id"],
                                         "finding": f"'{s}' is a synonym; prefer canonical term {c['name']}"})
        return findings

    # ------------------------------------------------------------ export
    def export_turtle(self) -> str:
        base = "https://factory.example/ontology#"
        lines = [f"@prefix : <{base}> .", "@prefix owl: <http://www.w3.org/2002/07/owl#> .",
                 "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
                 "@prefix skos: <http://www.w3.org/2004/02/skos/core#> .",
                 "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .", ""]
        xsd = {"string": "xsd:string", "integer": "xsd:integer", "number": "xsd:decimal", "boolean": "xsd:boolean",
               "date": "xsd:date", "datetime": "xsd:dateTime", "money": "xsd:integer", "identifier": "xsd:string",
               "enum": "xsd:string"}
        esc = lambda s: (s or "").replace("\\", "\\\\").replace('"', '\\"')
        for c in self.concepts(include_deprecated=True):
            lines.append(f":{c['name']} a owl:Class ;")
            lines.append(f'    rdfs:label "{c["name"]}" ;')
            lines.append(f'    skos:definition "{esc(c["definition"])}" ;')
            for s in c.get("synonyms", []):
                lines.append(f'    skos:altLabel "{esc(s)}" ;')
            if c.get("standard_ref"):
                lines.append(f"    rdfs:seeAlso <{c['standard_ref']}> ;")
            if c["status"] == "deprecated":
                lines.append("    owl:deprecated true ;")
            lines.append(f'    :dataClass "{c["data_class"]}" ;')
            lines.append(f'    :version {c["version"]} .')
            for a in c.get("attributes", []):
                lines += [f":{c['name']}_{a['name']} a owl:DatatypeProperty ;", f"    rdfs:domain :{c['name']} ;",
                          f"    rdfs:range {xsd.get(a['type'], 'xsd:string')} ;", f'    :dataClass "{a["data_class"]}" .']
            for r in c.get("relations", []):
                lines += [f":{r['type']} a owl:ObjectProperty ;", f"    rdfs:domain :{c['name']} ;",
                          f"    rdfs:range :{r['target']} ;", f'    :cardinality "{r["cardinality"]}" .']
            for r in c.get("rules", []):
                lines += [f":{r['id']} a :BusinessRule ;", f"    :appliesTo :{c['name']} ;", f'    rdfs:label "{esc(r["name"])}" ;',
                          f'    :expression "{esc(r["expression"])}" ;', f'    :severity "{r["severity"]}" ;',
                          f'    :status "{r.get("status", "active")}" ;', f'    rdfs:comment "{esc(r["message"])}" .']
            for a in c.get("actions", []):
                lines += [f":{a['name']} a :ActionType ;", f"    :onConcept :{c['name']} ;",
                          *([f"    :creates :{a['creates']} ;"] if a.get("creates") else []),
                          *[f"    :precondition :{rid} ;" for rid in a.get("preconditions", [])],
                          f'    :sideEffect "{a["side_effect"]}" ;', f'    :status "{a.get("status", "active")}" ;',
                          f'    rdfs:comment "{esc(a["description"])}" .']
            lines.append("")
        return "\n".join(lines)

    def graph(self) -> dict:
        nodes = [{"id": c["name"], "data_class": c["data_class"], "version": c["version"]} for c in self.concepts()]
        edges = [{"source": c["name"], "target": r["target"], "type": r["type"], "cardinality": r["cardinality"]}
                 for c in self.concepts() for r in c.get("relations", [])]
        return {"nodes": nodes, "edges": edges}
