"""OSFI regulatory reporting product: lakehouse (MCP) -> data quality -> calculation -> edit checks ->
variance and commentary -> certification -> maker-checker filing, all with cell-level lineage.

Design rules (see catalog/domains/osfi-regulatory-reporting):
- Source data is read ONLY through certified, read-only Unity Catalog functions on the approved Databricks
  MCP server, by a workload identity. Every read pins a Delta table version; the snapshot id is the digest of
  those versions, so a filed return can be reproduced exactly.
- Figures come from template formulas evaluated by the ontology's safe expression evaluator, never from a
  model. Agents draft variance wording and data-quality triage only; drivers are confirmed by the preparer.
- Certification and filing are ontology actions. Rules block or route to named approvers; filing is tier 3
  and needs the reviewer and a senior officer bound to the exact package digest.
"""
from __future__ import annotations

import calendar
import copy
import datetime as dt
import re
from pathlib import Path

import yaml

from ..agents.runtime import Agent
from ..core.db import unj
from ..core.util import REPO_ROOT, digest_obj, new_id
from ..ontology import rules as safe_expr
from ..ontology.actions import ActionDenied
from ..tools.gateway import CallContext

TENANT, PROJECT = "finance", "osfi-regulatory-reporting"
DOMAIN = REPO_ROOT / "catalog" / "domains" / PROJECT
SRC_RE = re.compile(r"^(?P<action>\w+)\.(?P<field>\w+)(?:\[(?P<idx>\d+)\])?$")
NUM_RE = re.compile(r"(?<![\w-])-?\d[\d,]*(?:\.\d+)?%?")
DRIVER_CLAIM_RE = re.compile(r"\b(due to|because|driven by|as a result of|owing to)\b", re.I)


# ---------------------------------------------------------------- configuration
def domain() -> dict:
    return yaml.safe_load((DOMAIN / "project.yaml").read_text(encoding="utf-8"))


def template(code: str) -> dict:
    return yaml.safe_load((DOMAIN / "returns" / f"{code}.yaml").read_text(encoding="utf-8"))


def dq_config() -> dict:
    return yaml.safe_load((DOMAIN / "dq.yaml").read_text(encoding="utf-8"))


def entity() -> dict:
    return yaml.safe_load((REPO_ROOT / "samples" / "osfi" / "lakehouse.yaml").read_text(encoding="utf-8"))["entity"]


def month_end(d: dt.date, months_back: int) -> dt.date:
    y, m = d.year, d.month - months_back
    while m <= 0:
        y, m = y - 1, m + 12
    return dt.date(y, m, calendar.monthrange(y, m)[1])


def prior_period(tpl: dict, period_end: str) -> str:
    back = 3 if tpl["frequency"] == "quarterly" else 1
    return month_end(dt.date.fromisoformat(period_end), back).isoformat()


def requirement_pct(tpl: dict, ent: dict) -> float:
    r = tpl["requirement"]
    buffer = r.get("dsib_buffer_share_of_hla", 0.0) * ent["hla_surcharge_pct"] if ent.get("dsib") else 0.0
    return round(r["minimum_pct"] + buffer, 3)


# ---------------------------------------------------------------- 1. extraction through MCP
def extract(f, tpl: dict, period_end: str, versions: dict | None = None, run_id: str = "regrep", env: str = "nonprod") -> dict:
    ctx = CallContext(f.directory.get(domain()["data_source"]["reader_identity"]), TENANT, PROJECT, run_id, env,
                      purpose="product")
    srcs = {n: SRC_RE.match(s).groupdict() for n, s in tpl["sources"].items()}
    actions = sorted({s["action"] for s in srcs.values()} | {"load_log"})
    reads = {}
    for a in actions:
        args = {"period_end": period_end, **({"version": versions[a]} if versions and a in versions else {})}
        reads[a] = f.tools.invoke(ctx, "lakehouse", a, args)
    inputs, lineage = {}, {}
    for name, s in srcs.items():
        vals = reads[s["action"]].get("values") or {}
        v = vals.get(s["field"])
        if v is not None and s["idx"] is not None:
            v = v[int(s["idx"])]
        inputs[name] = v
        lineage[name] = {"table": reads[s["action"]]["table"], "version": reads[s["action"]]["version"],
                         "field": s["field"] + (f"[{s['idx']}]" if s["idx"] else "")}
    snapshot = {a: {"table": r["table"], "version": r["version"]} for a, r in reads.items()}
    sid = "SNAP-" + digest_obj([tpl["code"], tpl["template_version"], period_end, snapshot]).split(":")[-1][:16]
    return {"period_end": period_end, "inputs": inputs, "lineage": lineage, "snapshot": snapshot, "snapshot_id": sid,
            "load_log": reads["load_log"].get("values") or {}, "owners": {r["table"]: r["owner"] for r in reads.values()},
            "versions": {a: r["version"] for a, r in reads.items()}}


# ---------------------------------------------------------------- 2. data quality (BCBS 239 dimensions)
def data_quality(ext: dict, tpl: dict, cfg: dict) -> list[dict]:
    breaks = []
    pe = dt.date.fromisoformat(ext["period_end"])
    used = sorted({ext["lineage"][n]["table"] for n in ext["inputs"]})
    for name, v in ext["inputs"].items():
        if v is None:
            breaks.append({"check": "DQ-COMPLETENESS", "severity": "critical", "table": ext["lineage"][name]["table"],
                           "detail": f"{name} missing for {ext['period_end']}"})
        elif not isinstance(v, (int, float)) or isinstance(v, bool):
            breaks.append({"check": "DQ-VALIDITY", "severity": "critical", "table": ext["lineage"][name]["table"],
                           "detail": f"{name} is not numeric"})
    for table in used:
        entry = ext["load_log"].get(table.split(".")[-1])
        if not entry or entry[0] != "complete":
            breaks.append({"check": "DQ-LOAD-STATUS", "severity": "critical", "table": table,
                           "detail": f"load for {ext['period_end']} is {entry[0] if entry else 'missing'}"})
        elif (dt.date.fromisoformat(entry[1]) - pe).days > cfg["cutoff_days_after_period_end"]:
            breaks.append({"check": "DQ-TIMELINESS", "severity": "warning", "table": table,
                           "detail": f"loaded {entry[1]}, after the {cfg['cutoff_days_after_period_end']}-day cut-off"})
    for c in cfg["checks"]:
        if c["id"] == "DQ-RECONCILIATION" and c.get("applies_to") == tpl["code"]:
            left, right = ext["inputs"].get(c["left"]), ext["inputs"].get(c["right"])
            if isinstance(left, (int, float)) and isinstance(right, (int, float)) and right:
                diff = round(100.0 * abs(left - right) / abs(right), 3)
                sev = "critical" if diff > c["critical_pct"] else "warning" if diff > c["warn_pct"] else None
                if sev:
                    breaks.append({"check": c["id"], "severity": sev, "table": ext["lineage"][c["left"]]["table"],
                                   "detail": f"{c['left']} differs from {c['right']} by {diff}%"})
    for b in breaks:
        b["owner"] = ext["owners"].get(b["table"], "data-platform")
    return breaks


# ---------------------------------------------------------------- 3. calculation, edit checks, lineage
def calculate(tpl: dict, inputs: dict) -> tuple[list[dict], dict]:
    env, cells = dict(inputs), []
    for c in tpl["cells"]:
        try:
            v = safe_expr.compute(c["formula"], env)
            v = round(float(v), 3) if c.get("ratio") else round(float(v), 2)
        except (safe_expr.UnknownValue, safe_expr.RuleSyntaxError, ZeroDivisionError, TypeError):
            v = None
        env[c["key"]] = v
        cells.append({"id": c["id"], "key": c["key"], "label": c["label"], "value": v, "formula": c["formula"],
                      "ratio": bool(c.get("ratio"))})
    return cells, env


def cell_lineage(tpl: dict, ext: dict) -> dict:
    keys = {c["key"]: c for c in tpl["cells"]}

    def sources(name, seen=()):
        if name in ext["lineage"]:
            return [{"input": name, **ext["lineage"][name]}]
        if name in keys and name not in seen:
            out = []
            for n in sorted(safe_expr.names(keys[name]["formula"])):
                out += sources(n, seen + (name,))
            return out
        return []
    return {c["id"]: sources(c["key"]) for c in tpl["cells"]}


def edit_checks(tpl: dict, env: dict) -> list[dict]:
    out = []
    for ec in tpl["edit_checks"]:
        try:
            ok = bool(safe_expr.evaluate(ec["expression"], env))
        except (safe_expr.UnknownValue, safe_expr.RuleSyntaxError, ZeroDivisionError, TypeError):
            ok = False
        out.append({"id": ec["id"], "name": ec["name"], "passed": ok, "scope": ec.get("scope", "intra-return")})
    return out


def variances(tpl: dict, cells: list[dict], prior_cells: list[dict]) -> list[dict]:
    prior = {c["id"]: c["value"] for c in prior_cells}
    cfg, out = tpl["variance"], []
    for c in cells:
        p, v = prior.get(c["id"]), c["value"]
        if c["ratio"] or p in (None, 0) or v is None:
            continue
        delta = round(v - p, 2)
        pct = round(100.0 * delta / abs(p), 1)
        if abs(pct) >= cfg["material_pct"] and abs(delta) >= cfg["material_abs"]:
            out.append({"cell_id": c["id"], "label": c["label"], "prior": p, "current": v, "delta": delta, "pct": pct})
    return out


def numbers_in(text: str) -> list[float]:
    return [round(float(m.strip("%").replace(",", "")), 1) for m in NUM_RE.findall(text or "")]


def ungrounded(text: str, figures: list[dict]) -> list[float]:
    allowed = set()
    for v in figures:
        for k in ("prior", "current", "delta", "pct"):
            allowed |= {round(float(v[k]), 1), round(abs(float(v[k])), 1)}
    return [n for n in numbers_in(text) if n not in allowed and abs(n) not in allowed]


# ---------------------------------------------------------------- workflow
class RegReportingApp:
    def __init__(self, f, env: str = "nonprod", today: str | None = None):
        self.f, self.env = f, env
        self.today = dt.date.fromisoformat(today) if today else dt.date.today()
        self.runs: dict[str, dict] = {}
        self.analyst = self._agent("reg-variance-analyst")
        self.triage = self._agent("reg-dq-triage")

    def _agent(self, aid: str) -> Agent:
        row = self.f.db.one("SELECT manifest FROM agents WHERE id=?", (aid,))
        if not row:
            raise RuntimeError(f"agent {aid} is not registered; run bootstrap")
        return Agent(self.f, copy.deepcopy(unj(row["manifest"])))

    def _role(self, user: str, role: str):
        p = self.f.directory.get(user)
        if not (p.kind == "human" and p.has_role(role) and p.in_tenant(TENANT)):
            raise PermissionError(f"{user} is not a {role} in tenant {TENANT}")
        return p

    def _compute(self, tpl, period_end, versions=None, run_id="regrep"):
        ext = extract(self.f, tpl, period_end, versions, run_id, self.env)
        cells, env = calculate(tpl, ext["inputs"])
        return ext, cells, env

    # ---- prepare: extract, DQ, calculate, validate, variance, commentary drafts
    def prepare(self, preparer: str, code: str, period_end: str, versions: dict | None = None) -> dict:
        self._role(preparer, "reg_preparer")
        tpl, ent, rid = template(code), entity(), new_id("RRUN")
        ext, cells, env = self._compute(tpl, period_end, versions, rid)
        breaks = data_quality(ext, tpl, dq_config())
        checks = edit_checks(tpl, env)
        lineage = cell_lineage(tpl, ext)
        lineage_ok = all(lineage[c["id"]] and all(s["version"] for s in lineage[c["id"]]) for c in cells) and \
            all(ext["inputs"][s["input"]] is not None for ss in lineage.values() for s in ss)
        prior_ext, prior_cells, _ = self._compute(tpl, prior_period(tpl, period_end), None, rid)
        var = variances(tpl, cells, prior_cells)
        run = {"run_id": rid, "return_code": code, "template_version": tpl["template_version"], "period_end": period_end,
               "entity": ent["id"], "preparer": preparer, "snapshot": ext["snapshot"], "snapshot_id": ext["snapshot_id"],
               "versions": ext["versions"], "cells": cells, "headline": env.get(tpl["headline"]),
               "floor": requirement_pct(tpl, ent), "dq": breaks, "edit_checks": checks, "lineage": lineage,
               "lineage_complete": lineage_ok, "variances": var, "explanations": {}, "issues": [],
               "prior_snapshot_id": prior_ext["snapshot_id"],
               "deadline": (dt.date.fromisoformat(period_end) + dt.timedelta(days=tpl["filing_days"])).isoformat()}
        if any(b["severity"] == "critical" for b in breaks):
            t = self.triage.run(None, {"kind": "regrep.dq_triage", "breaks": breaks, "snapshot_id": ext["snapshot_id"]},
                                tenant=TENANT, env=self.env, purpose="product.regreport.dq", data_class="confidential")
            run["triage"] = t.payload.get("items", []) if t.status == "success" else []
            run["status"] = "dq_blocked"
        else:
            run["status"] = self._draft_commentary(run)
        self.runs[rid] = run
        self.f.ledger.record("regrep.prepared", rid, preparer,
                             {k: run[k] for k in ("return_code", "period_end", "snapshot_id", "status", "headline", "floor")}
                             | {"dq": len(breaks), "edit_failures": sum(not c["passed"] for c in checks)}, PROJECT)
        return run

    def _draft_commentary(self, run) -> str:
        if not run["variances"]:
            return "ready_to_certify"
        r = self.analyst.run(None, {"kind": "regrep.variance", "variances": run["variances"],
                                    "snapshot_id": run["snapshot_id"]},
                             tenant=TENANT, env=self.env, purpose="product.regreport.variance", data_class="confidential")
        drafts = {d["cell_id"]: d["draft"] for d in (r.payload.get("commentary", []) if r.status == "success" else [])}
        for v in run["variances"]:
            d = drafts.get(v["cell_id"], "")
            bad = ungrounded(d, [v])
            if bad:
                run["issues"].append(f"{v['cell_id']}: draft cites numbers not in the calculation {bad}")
                d = ""
            if DRIVER_CLAIM_RE.search(d):
                run["issues"].append(f"{v['cell_id']}: draft asserts a business driver; drivers come from the preparer")
                d = ""
            v["draft"] = d
        return "awaiting_explanations"

    def explain(self, preparer: str, run_id: str, cell_id: str, driver: str) -> dict:
        run = self.runs[run_id]
        if preparer != run["preparer"]:
            raise PermissionError("only the preparer of this run can explain its variances")
        if cell_id not in {v["cell_id"] for v in run["variances"]}:
            raise KeyError(f"{cell_id} has no material variance")
        if len(driver.strip()) < 20:
            raise ValueError("the driver explanation must be at least 20 characters")
        run["explanations"][cell_id] = driver.strip()
        if run["status"] == "awaiting_explanations" and len(run["explanations"]) == len(run["variances"]):
            run["status"] = "ready_to_certify"
        return run

    def unexplained(self, run) -> int:
        return sum(1 for v in run["variances"] if v["cell_id"] not in run["explanations"])

    # ---- package (what is certified and filed, byte for byte)
    def package(self, run: dict) -> dict:
        commentary = [{"cell_id": v["cell_id"], "text": (v.get("draft", "") + " Driver: " + run["explanations"][v["cell_id"]]).strip()}
                      for v in run["variances"] if v["cell_id"] in run["explanations"]]
        pkg = {"entity": run["entity"], "return_code": run["return_code"], "template_version": run["template_version"],
               "period_end": run["period_end"], "snapshot": run["snapshot"], "snapshot_id": run["snapshot_id"],
               "cells": [{"id": c["id"], "value": c["value"]} for c in run["cells"]],
               "headline": run["headline"], "requirement": run["floor"], "commentary": commentary,
               "dq": [{k: b[k] for k in ("check", "severity", "detail")} for b in run["dq"]],
               "edit_checks": run["edit_checks"], "lineage_digest": digest_obj(run["lineage"])}
        digest = digest_obj(pkg).split(":")[-1]
        return {"package": pkg, "digest": digest,
                "signature": self.f.keys.sign(domain()["signing_identity"], digest.encode())}

    # ---- certification and filing (ontology actions)
    def certify(self, preparer: str, run_id: str, amendment_reason: str = "", request_id: str | None = None) -> dict:
        self._role(preparer, "reg_preparer")
        if request_id:
            req = self.f.actions.request(request_id)
            return self.f.actions.execute("CertifyReturn", preparer, req["subject"], req["params"], env=self.env,
                                          tenant=TENANT, request_id=request_id)
        run = self.runs[run_id]
        pkg = self.package(run)
        run["package"] = pkg
        prior_cert = self.f.db.one("SELECT certification_id FROM return_registry_sor WHERE return_code=? AND period_end=? "
                                   "AND certification_id IN (SELECT certification_id FROM filings_sor)",
                                   (run["return_code"], run["period_end"]))
        params = {"return_code": run["return_code"], "period_end": run["period_end"],
                  "template_version": run["template_version"], "snapshot_id": run["snapshot_id"],
                  "package_digest": pkg["digest"],
                  "dq_critical_breaks": sum(b["severity"] == "critical" for b in run["dq"]),
                  "dq_warnings": sum(b["severity"] == "warning" for b in run["dq"]),
                  "edit_check_failures": sum(not c["passed"] for c in run["edit_checks"]),
                  "unexplained_variances": self.unexplained(run), "lineage_complete": bool(run["lineage_complete"]),
                  "headline_ratio_pct": float(run["headline"] if run["headline"] is not None else 0.0),
                  "regulatory_floor_pct": float(run["floor"]),
                  "days_to_deadline": (dt.date.fromisoformat(run["deadline"]) - self.today).days,
                  "is_amendment": bool(prior_cert), "amendment_reason": amendment_reason}
        out = self.f.actions.execute("CertifyReturn", preparer, {"id": run["entity"], "tenant": TENANT}, params,
                                     env=self.env, tenant=TENANT)
        run["certification_request"] = out.get("request_id")
        return out

    def filing_ticket(self, preparer: str, cert_request_id: str) -> dict:
        self._role(preparer, "reg_preparer")
        cert = self.f.actions.request(cert_request_id)
        if cert["action"] != "CertifyReturn" or cert["status"] != "executed":
            raise ActionDenied(f"{cert_request_id} is not an executed certification")
        cid = cert["result"]["certification_id"]
        if self.f.db.one("SELECT filing_id FROM filings_sor WHERE certification_id=?", (cid,)):
            raise ActionDenied(f"certification {cid} has already been filed")
        p = cert["params"]
        params = {"certification_id": cid, "return_code": p["return_code"], "period_end": p["period_end"],
                  "package_digest": p["package_digest"]}
        return {"subject": cert["subject"], "params": params,
                "binding": self.f.actions.tool_binding("FileReturn", cert["subject"], params, self.env, TENANT)}

    def file(self, preparer: str, cert_request_id: str, approvals: list[str]) -> dict:
        t = self.filing_ticket(preparer, cert_request_id)
        return self.f.actions.execute("FileReturn", preparer, t["subject"], t["params"], env=self.env, tenant=TENANT,
                                      tool_approvals=approvals)

    # ---- reproduction for audit
    def reproduce(self, run_id: str) -> dict:
        run = self.runs[run_id]
        tpl = template(run["return_code"])
        ext, cells, _ = self._compute(tpl, run["period_end"], run["versions"], "reproduce")
        same = [c["value"] for c in cells] == [c["value"] for c in run["cells"]] and ext["snapshot_id"] == run["snapshot_id"]
        self.f.ledger.record("regrep.reproduced", run_id, "auditor", {"identical": same, "snapshot": ext["snapshot_id"]},
                             PROJECT)
        return {"identical": same, "snapshot_id": ext["snapshot_id"]}


# ---------------------------------------------------------------- evaluation (Pipeline C for the product agents)
def evaluate(f, app: RegReportingApp) -> dict:
    suite = yaml.safe_load((DOMAIN / "evals.yaml").read_text(encoding="utf-8"))
    results: dict[str, list[bool]] = {}
    for case in suite["cases"]:
        ok = False
        if case["kind"] == "calculation":
            _, cells, env = app._compute(template(case["return"]), case["period_end"], case.get("versions"))
            ok = abs(env[case["cell"]] - case["expect"]) <= case.get("tolerance", 0.001)
        elif case["kind"] == "commentary":
            run = app.prepare(case["preparer"], case["return"], case["period_end"], case.get("versions"))
            ok = run["status"] == "awaiting_explanations" and not run["issues"] and \
                all(v.get("draft") and not DRIVER_CLAIM_RE.search(v["draft"]) for v in run["variances"])
        elif case["kind"] == "dq_triage":
            run = app.prepare(case["preparer"], case["return"], case["period_end"], case.get("versions"))
            owners = {i["owner"] for i in run.get("triage", [])}
            ok = run["status"] == "dq_blocked" and case["expect_owner"] in owners
        elif case["kind"] == "grounding":
            ok = bool(ungrounded(case["text"], [case["figures"]])) == case["expect_ungrounded"]
        elif case["kind"] == "reproducibility":
            run = app.prepare(case["preparer"], case["return"], case["period_end"], case.get("versions"))
            ok = app.reproduce(run["run_id"])["identical"]
        results.setdefault(case["category"], []).append(ok)
    scores = {k: round(sum(v) / len(v), 3) for k, v in results.items()}
    critical = [k for k in suite["critical_categories"] if scores.get(k, 0.0) < 1.0]
    avg = round(sum(scores.values()) / len(scores), 3)
    gate = "pass" if not critical and avg >= suite["min_average"] else "fail"
    f.ledger.record("eval.regrep", PROJECT, "eval-harness", {"scores": scores, "critical_failures": critical, "gate": gate},
                    PROJECT)
    return {"scores": scores, "average": avg, "critical_failures": critical, "gate": gate}
