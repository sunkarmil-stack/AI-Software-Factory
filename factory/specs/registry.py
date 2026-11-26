"""Specification registry — Pipeline A gates (REQ 002, 004, 005, 006).

submit()   : schema + completeness + contradiction review; creates trace nodes.
baseline() : requires bound approvals for the risk tier; supersedes the prior baseline and
             invalidates dependent plans, tests, evidence and approvals.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import jsonschema

from ..core.approvals import ApprovalService
from ..core.db import DB, j, unj
from ..core.evidence import Ledger
from ..core.util import digest_obj, now, new_id
from .schema import SPEC_SCHEMA, SUBSTANTIVE
from .trace import Trace


@dataclass
class SpecResult:
    status: str  # accepted | blocked
    digest: str = ""
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    review_items: list[str] = field(default_factory=list)
    unresolved_decisions: list[str] = field(default_factory=list)


def _get(d: dict, path: str):
    for p in path.split("."):
        d = d.get(p) if isinstance(d, dict) else None
    return d


def baseline_roles(risk_tier: int) -> list[str]:
    return ["product_owner"] if risk_tier <= 1 else ["product_owner", "architect"]


class SpecRegistry:
    def __init__(self, db: DB, ledger: Ledger, trace: Trace, approvals: ApprovalService):
        self.db, self.ledger, self.trace, self.approvals = db, ledger, trace, approvals

    # ---------- analysis ----------
    def completeness(self, spec: dict) -> tuple[list[str], list[str]]:
        missing, errors = [], []
        for req in SPEC_SCHEMA["required"]:
            if req not in spec:
                missing.append(req)
        v = jsonschema.Draft202012Validator(SPEC_SCHEMA)
        for e in v.iter_errors(spec):
            if e.validator == "required":
                for f in e.validator_value:
                    if isinstance(e.instance, dict) and f not in e.instance:
                        path = ".".join(str(x) for x in e.absolute_path)
                        name = f"{path}.{f}" if path else f
                        if name not in missing:
                            missing.append(name)
            else:
                errors.append(f"{'.'.join(str(x) for x in e.absolute_path) or '<root>'}: {e.message}")
        for path in SUBSTANTIVE:
            val = _get(spec, path)
            if val is not None and not val and path not in missing:
                missing.append(path)
        return sorted(set(missing)), errors

    def contradictions(self, spec: dict) -> list[dict]:
        seen: dict[str, tuple[str, int]] = {}
        out = []
        for r in spec.get("requirements", []):
            for ac in r.get("acceptance_criteria", []):
                req = ac.get("check", {}).get("request", {})
                key = digest_obj({"m": req.get("method"), "p": req.get("path"), "h": req.get("headers", {}),
                                  "b": req.get("body")})
                status = ac.get("check", {}).get("expect", {}).get("status")
                if key in seen and seen[key][1] != status:
                    out.append({"a": seen[key][0], "b": ac["id"],
                                "detail": f"identical request expects {seen[key][1]} in {seen[key][0]} but "
                                          f"{status} in {ac['id']}"})
                else:
                    seen[key] = (ac["id"], status)
        return out

    def unresolved_decisions(self, spec: dict) -> list[str]:
        approved = {d["id"] for d in spec.get("decisions", []) if d.get("status") == "approved"}
        out = []
        for r in spec.get("requirements", []):
            for d in r.get("decisions", []):
                if d not in approved:
                    out.append(f"{r['id']} -> {d}")
        return out

    # ---------- lifecycle ----------
    def submit(self, spec: dict, actor: str) -> SpecResult:
        missing, errors = self.completeness(spec)
        project = spec.get("project", "unknown")
        if missing or errors:
            self.ledger.record("spec.blocked", spec.get("spec_id", "?"), actor,
                               {"missing": missing, "errors": errors[:20]}, project)
            return SpecResult("blocked", missing=missing, errors=errors)
        digest = digest_obj(spec)
        items = []
        for c in self.contradictions(spec):
            rid = new_id("RVW")
            self.db.execute("INSERT INTO review_items(id,project,kind,ref,summary,status,data,created) VALUES(?,?,?,?,?,?,?,?)",
                            (rid, project, "contradiction", spec["spec_id"], c["detail"], "open", j(c), now()))
            items.append(rid)
        unresolved = self.unresolved_decisions(spec)
        self.db.execute("INSERT OR REPLACE INTO specs(spec_id,version,digest,body,status,project,created,created_by)"
                        " VALUES(?,?,?,?,?,?,?,?)",
                        (spec["spec_id"], spec["version"], digest, j(spec), "draft", project, now(), actor))
        self.ledger.record("spec.submitted", f"{spec['spec_id']}@v{spec['version']}", actor,
                           {"digest": digest, "review_items": items, "unresolved_decisions": unresolved}, project)
        status = "blocked" if items else "accepted"
        return SpecResult(status, digest=digest, review_items=items, unresolved_decisions=unresolved)

    def get(self, spec_id: str, version: int | None = None) -> dict | None:
        if version is None:
            r = self.db.one("SELECT * FROM specs WHERE spec_id=? ORDER BY version DESC LIMIT 1", (spec_id,))
        else:
            r = self.db.one("SELECT * FROM specs WHERE spec_id=? AND version=?", (spec_id, version))
        if r:
            r["body"] = unj(r["body"])
        return r

    def current_baseline(self, spec_id: str) -> dict | None:
        r = self.db.one("SELECT * FROM specs WHERE spec_id=? AND status='baselined' ORDER BY version DESC LIMIT 1",
                        (spec_id,))
        if r:
            r["body"] = unj(r["body"])
        return r

    def baseline_binding(self, spec_id: str, version: int) -> dict:
        s = self.get(spec_id, version)
        return ApprovalService.binding("spec.baseline", s["digest"], "n/a", f"{spec_id}@v{version}")

    def baseline(self, spec_id: str, version: int, approval_ids: list[str], actor: str) -> dict:
        s = self.get(spec_id, version)
        if not s:
            raise KeyError(spec_id)
        open_items = self.db.all("SELECT id FROM review_items WHERE ref=? AND status='open'", (spec_id,))
        if open_items:
            raise PermissionError(f"open review items block baseline: {[i['id'] for i in open_items]}")
        spec = s["body"]
        self.approvals.require(approval_ids, self.baseline_binding(spec_id, version),
                               baseline_roles(spec["risk_tier"]))
        prev = self.current_baseline(spec_id)
        project = s["project"]
        spec_node = self.trace.node("SPEC", f"{spec_id}@v{version}", s["digest"], project)
        new_nodes: dict[str, str] = {}
        for bo in spec["business_outcomes"]:
            new_nodes[bo["id"]] = self.trace.node("BO", bo["id"], digest_obj(bo), project, bo)
        for r in spec["requirements"]:
            rd = {k: v for k, v in r.items() if k != "acceptance_criteria"}
            rn = self.trace.node("REQ", r["id"], digest_obj(rd), project, rd)
            new_nodes[r["id"]] = rn
            self.trace.edge(rn, new_nodes.get(r["outcome"], ""), "realizes")
            for ac in r["acceptance_criteria"]:
                an = self.trace.node("AC", ac["id"], digest_obj(ac), project, ac)
                new_nodes[ac["id"]] = an
                self.trace.edge(an, rn, "verifies")
                self.trace.edge(spec_node, an, "contains")
        affected: list[str] = []
        if prev:
            self.db.execute("UPDATE specs SET status='superseded' WHERE spec_id=? AND version=?", (spec_id, prev["version"]))
            for n in self.db.all("SELECT * FROM trace_nodes WHERE project=? AND kind IN ('BO','REQ','AC') AND status='current'",
                                 (project,)):
                if n["id"] not in new_nodes.values():
                    affected += self.trace.supersede(n["id"], f"changed in {spec_id}@v{version}")
            old_spec_node = self.trace.current("SPEC", f"{spec_id}@v{prev['version']}", project)
            if old_spec_node:
                self.db.execute("UPDATE trace_nodes SET status='superseded' WHERE id=?", (old_spec_node["id"],))
            for nid in affected:
                n = self.trace.get(nid)
                if n and n["kind"] == "APPROVAL":
                    self.approvals.revoke(n["ref"], "spec-registry", f"stale after {spec_id}@v{version}")
        self.db.execute("UPDATE specs SET status='baselined' WHERE spec_id=? AND version=?", (spec_id, version))
        self.ledger.record("spec.baselined", f"{spec_id}@v{version}", actor,
                           {"approvals": approval_ids, "digest": s["digest"], "stale": affected}, project)
        return {"spec_node": spec_node, "stale": affected}

    def changed_requirements(self, spec_id: str, old_v: int, new_v: int) -> list[str]:
        a, b = self.get(spec_id, old_v)["body"], self.get(spec_id, new_v)["body"]
        da = {r["id"]: digest_obj(r) for r in a["requirements"]}
        db_ = {r["id"]: digest_obj(r) for r in b["requirements"]}
        return sorted(k for k in set(da) | set(db_) if da.get(k) != db_.get(k))
