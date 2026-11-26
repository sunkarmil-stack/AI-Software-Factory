"""Offline (deterministic) behaviour for each logical agent role.

These handlers stand in for an LLM so that the governed pipeline is fully runnable and
testable without model access. Two handlers deliberately *emulate a model that follows
injected instructions* ("AGENT-INSTRUCTION:" / "SYSTEM OVERRIDE:" lines in source or
retrieved content). This lets the acceptance suite prove that containment comes from the
tool gateway, identity and policy — not from model refusal (REQ 038, ACC 02).
"""
from __future__ import annotations

import json
import re

from ..core.util import digest_obj
from ..models.gateway import offline_handler
from .codegen import generate_service, generate_tests

VAGUE = ["fast", "appropriate", "user-friendly", "as needed", "etc", "reasonable", "robust", "seamless", "quickly"]
INJECT = re.compile(r"(?:AGENT-INSTRUCTION|SYSTEM OVERRIDE):\s*(\{.*\})")


def env(status, summary, payload, refs, **kw):
    return {"status": status, "summary": summary, "payload": payload, "evidence_refs": refs, **kw}


def _injected_requests(texts: list[str]) -> list[dict]:
    reqs = []
    for t in texts:
        for m in INJECT.finditer(t or ""):
            try:
                reqs.append(json.loads(m.group(1)))
            except json.JSONDecodeError:
                pass
    return reqs


@offline_handler("spec.analyze")
def spec_analyze(task):
    spec = task["spec"]
    questions = []
    for r in spec["requirements"]:
        for w in VAGUE:
            if re.search(rf"\b{re.escape(w)}\b", r["statement"], re.I):
                questions.append({"ref": r["id"], "question": f"'{w}' is not measurable; specify a threshold"})
    open_asm = [a["id"] for a in spec.get("assumptions", []) if a["status"] != "validated"]
    for a in open_asm:
        questions.append({"ref": a, "question": "assumption not validated; confirm or schedule a spike"})
    status = "blocked" if questions else "success"
    return env(status, f"{len(questions)} clarification(s) needed" if questions else "specification is testable",
               {"gaps": questions, "requirement_count": len(spec["requirements"]),
                "ac_count": sum(len(r["acceptance_criteria"]) for r in spec["requirements"])},
               [digest_obj(spec)], questions=questions)


@offline_handler("architecture.propose")
def architecture_propose(task):
    spec = task["spec"]
    approved = {d["id"] for d in spec["decisions"] if d["status"] == "approved"}
    proposals = []
    for r in spec["requirements"]:
        for d in r.get("decisions", []):
            if d not in approved:
                proposals.append({"id": d, "context": f"needed by {r['id']}", "options": ["TBD-A", "TBD-B"],
                                  "decision": "unresolved", "status": "proposed"})
    threats = []
    for c in spec["contracts"]:
        for ep in c["endpoints"]:
            name = f"{ep['method']} {ep['path']}"
            threats += [
                {"id": f"TM-{len(threats)+1}", "stride": "Spoofing", "target": name,
                 "mitigation": "gateway token validation + service claim check"},
                {"id": f"TM-{len(threats)+2}", "stride": "Elevation of privilege", "target": name,
                 "mitigation": f"require_scope('{ep['required_scope']}') in handler; SAST rule"},
                {"id": f"TM-{len(threats)+3}", "stride": "Information disclosure", "target": name,
                 "mitigation": "tenant-filtered store access; 404 on foreign tenant"},
            ]
    return env("success", f"{len(threats)} threats, {len(proposals)} decisions to resolve",
               {"threats": threats, "decision_proposals": proposals}, [digest_obj(spec)])


@offline_handler("planning.plan")
def planning_plan(task):
    spec = task["spec"]
    tasks = [{"id": "TASK-1", "title": "Generate service routes, adapter and OpenAPI from contract",
              "requirements": [r["id"] for r in spec["requirements"]], "depends_on": [], "estimate_h": 2,
              "agent": "implementation"},
             {"id": "TASK-2", "title": "Propose unit test candidates from acceptance criteria",
              "requirements": [r["id"] for r in spec["requirements"]], "depends_on": ["TASK-1"], "estimate_h": 1,
              "agent": "test"},
             {"id": "TASK-3", "title": "Run tests in sandbox and open pull request",
              "requirements": [r["id"] for r in spec["requirements"]], "depends_on": ["TASK-1", "TASK-2"],
              "estimate_h": 0.5, "agent": "implementation"}]
    return env("success", f"{len(tasks)} bounded tasks", {"tasks": tasks}, [digest_obj(spec)])


@offline_handler("implementation.generate")
def implementation_generate(task):
    spec, contract = task["spec"], task["contract"]
    files = generate_service(spec, contract)
    if task.get("defect") == "drop_authz":  # used by REQ 036 test: a deliberately broken implementation
        files["app/routes.py"] = files["app/routes.py"].replace(
            '    c = require_scope(req, "accounts:read")\n    return 200, {"items"',
            '    from .security import caller\n    c = caller(req)\n    return 200, {"items"')
    injected = _injected_requests(list((task.get("workspace") or {}).values()))
    return env("success", f"generated {len(files)} files", {"files": files},
               [f"{contract['id']}@{contract['version']}"], tool_requests=injected)


@offline_handler("test.generate")
def test_generate(task):
    src = generate_tests(task["spec"], "Unit test candidates proposed by the test agent", "test_candidate")
    return env("success", "test candidates from acceptance criteria", {"files": {"tests/unit/test_generated.py": src}},
               [ac["id"] for r in task["spec"]["requirements"] for ac in r["acceptance_criteria"]])


@offline_handler("review.diff")
def review_diff(task):
    findings = []
    for path, text in task["files"].items():
        if INJECT.search(text) or re.search(r"(?i)ignore (all )?previous instructions", text):
            findings.append({"severity": "high", "file": path, "finding": "embedded instruction targeting agents (untrusted input)"})
        if re.search(r"(?i)(password|secret|api_key)\s*=\s*['\"][^'\"]{6,}", text):
            findings.append({"severity": "high", "file": path, "finding": "possible hard-coded secret"})
        if path.endswith("routes.py"):
            for m in re.finditer(r"def (\w+)\(req\):\n((?:    .*\n)+)", text):
                if "require_scope" not in m.group(2) and "healthz" not in m.group(1):
                    findings.append({"severity": "critical", "file": path, "finding": f"handler {m.group(1)} lacks authorization"})
    return env("success", f"{len(findings)} finding(s); advisory only — does not approve",
               {"findings": findings, "advisory": True}, [task.get("head_sha", "")])


@offline_handler("release.advise")
def release_advise(task):
    ev = task["evidence"]
    missing = [k for k, v in ev["checks"].items() if v != "passed"]
    rec = "do not promote" if missing or ev.get("stale") else "evidence complete; human authorization required"
    return env("success", rec, {"missing": missing, "recommendation": rec, "can_authorize": False},
               [ev.get("manifest_digest", "")])


@offline_handler("ops.triage")
def ops_triage(task):
    props = []
    for name, rows in task["metrics"].items():
        for row in rows:
            labels = json.loads(row["labels"]) if isinstance(row["labels"], str) else row["labels"]
            cat = labels.get("category")
            if cat and row["value"] >= task.get("threshold", 3):
                props.append({"title": f"Investigate {name} ({cat})", "category": cat, "count": row["value"],
                              "confidence": "medium", "uncertainty": "correlation from aggregate metrics only"})
    return env("success", f"{len(props)} proposal(s); no operational changes made", {"proposals": props},
               ["metrics-snapshot"])


@offline_handler("rag.answer")
def rag_answer(task):
    q = set(re.findall(r"[a-z0-9]+", task["question"].lower()))
    best, cites = [], []
    for p in task["passages"]:
        for sent in re.split(r"(?<=[.!?])\s+", p["text"]):
            overlap = len(q & set(re.findall(r"[a-z0-9]+", sent.lower())))
            if overlap:
                best.append((overlap, sent, p["citation"]))
    cfg = task.get("prompt_config", {})
    best.sort(key=lambda x: -x[0])
    chosen = best[:cfg.get("max_sentences", 2)]
    answer = " ".join(s for _, s, _ in chosen) if chosen else "I could not find this in the approved sources."
    cites = sorted({c for _, _, c in chosen})
    conf = min(1.0, (chosen[0][0] / max(len(q), 1)) if chosen else 0.0)
    injected = (_injected_requests([p["text"] for p in task["passages"]])
                if cfg.get("document_instruction_policy") == "follow" else [])
    return env("success", "answer drafted", {"answer": answer, "citations": cites, "confidence": round(conf, 2),
                                            "needs_review": conf < 0.35 or not cites},
               cites or ["no-sources"], tool_requests=injected)


# ---------------------------------------------------------------- wealth-advisory product agents
@offline_handler("wealth.review")
def wealth_review(task):
    s, cfg = task["snapshot"], task.get("prompt_config", {})
    thr = cfg.get("drift_threshold_pts", 2.0)
    notes = [f"Review of {s['account_id']} (snapshot {s['snapshot_id']}). Market value ${s['total_cents'] / 100:,.2f}."]
    for k, w in s["class_weights_pct"].items():
        if k in s["targets_pct"]:
            d = s["drift_pts"][k]
            where = "on target" if abs(d) < thr else (f"over target by {abs(d)} pts" if d > 0 else f"under target by {abs(d)} pts")
            notes.append(f"{k.replace('_', ' ')} {w}% vs target {s['targets_pct'][k]}% ({where}).")
    notes.append(f"KYC last updated {s['kyc']['age_days']} days ago; risk tolerance {s['kyc']['risk_tolerance']}.")
    proposals = []
    for k, d in sorted(s["drift_pts"].items(), key=lambda kv: -abs(kv[1])):
        if k == "cash" or abs(d) < thr or len(proposals) >= cfg.get("max_proposals", 3):
            continue
        amt = abs(s["rebalance_cents"][k])
        if d < 0:
            cands = sorted((p["risk_rating"], c) for c, p in s["shelf"].items()
                           if p["asset_class"] == k and p["kyp_reviewed"] and not p["proprietary"]
                           and p["risk_rating"] <= s["kyc"]["risk_tolerance"])
            if cands:
                proposals.append({"product_code": cands[0][1], "side": "buy", "amount_cents": amt,
                                  "basis": f"{k.replace('_', ' ')} is under target by {abs(d)} pts; amount from the calculation service."})
        else:
            held = sorted(((v, c) for c, v in s["position_cents"].items() if s["shelf"][c]["asset_class"] == k), reverse=True)
            if held:
                proposals.append({"product_code": held[0][1], "side": "sell", "amount_cents": amt,
                                  "basis": f"{k.replace('_', ' ')} is over target by {abs(d)} pts; amount from the calculation service."})
    return env("success", f"review prepared with {len(proposals)} draft proposal(s)",
               {"review_notes": " ".join(notes), "proposals": proposals, "snapshot_id": s["snapshot_id"]}, [s["snapshot_id"]])


@offline_handler("wealth.suitability")
def wealth_suitability(task):
    x, p = task["facts"], task["proposal"]
    findings, route = [], "proceed"
    if x["kyc_age_days"] > 365:
        findings.append("KYC is out of date; update with the client before recommending."); route = "do_not_proceed"
    if not x["product_approved"]:
        findings.append("Product has not passed know-your-product review."); route = "do_not_proceed"
    if x["conflict_of_interest"] and not x["conflict_disclosed"]:
        findings.append("Proprietary product: disclose the conflict to the client first."); route = "do_not_proceed"
    if route != "do_not_proceed":
        if x["product_risk"] > x["client_risk_tolerance"]:
            findings.append("Product risk is above the client's tolerance; supervisor review."); route = "escalate"
        if x["post_trade_concentration_pct"] > x["ips_max_concentration_pct"]:
            findings.append("Post-trade position exceeds the IPS concentration limit; portfolio manager review."); route = "escalate"
        if x["conflict_of_interest"]:
            findings.append("Proprietary product: compliance review."); route = "escalate"
    draft = (f"Recommend a {p['side']} of {p['product_code']}: product risk {x['product_risk']} against client tolerance "
             f"{x['client_risk_tolerance']}, post-trade weight {x['post_trade_concentration_pct']}% against the IPS limit of "
             f"{x['ips_max_concentration_pct']}%, KYC updated {x['kyc_age_days']} days ago.")
    return env("success", f"suitability: {route}", {"findings": findings or ["No exceptions found."],
                                                    "rationale_draft": draft, "suggested_route": route},
               [task["snapshot_id"]])


# ---------------------------------------------------------------- OSFI regulatory reporting product agents
@offline_handler("regrep.variance")
def regrep_variance(task):
    out = []
    for v in task["variances"]:
        direction = "increased" if v["delta"] > 0 else "decreased"
        out.append({"cell_id": v["cell_id"],
                    "draft": f"{v['label']} {direction} by {abs(v['delta']):,.0f} ({abs(v['pct'])}%), from "
                             f"{v['prior']:,.0f} to {v['current']:,.0f} (CAD millions)."})
    return env("success", f"{len(out)} variance draft(s)", {"commentary": out}, [task["snapshot_id"]])


@offline_handler("regrep.dq_triage")
def regrep_dq_triage(task):
    causes = {"DQ-COMPLETENESS": ("missing data for the period", "data owner restores the missing records"),
              "DQ-LOAD-STATUS": ("source load did not complete", "data owner re-runs the load"),
              "DQ-TIMELINESS": ("source loaded after the cut-off", "data owner confirms the late load is final"),
              "DQ-VALIDITY": ("non-numeric or malformed value", "data owner corrects the value at source"),
              "DQ-RECONCILIATION": ("difference against the general ledger", "finance and data owner reconcile")}
    items = []
    for b in task["breaks"]:
        cause, step = causes.get(b["check"], ("unclassified break", "data owner investigates"))
        items.append({"check": b["check"], "table": b["table"], "likely_cause": cause, "owner": b["owner"],
                      "next_step": f"{step}; the fix lands as a new table version and the return is re-run"})
    return env("success", f"{len(items)} break(s) triaged", {"items": items}, [task["snapshot_id"]])


# ---------------------------------------------------------------- retail account risk product agents
@offline_handler("risk.investigate")
def risk_investigate(task):
    a = task["alert"]
    lines = [f"{r['code']}: {r['detail']}." for r in sorted(a["reasons"], key=lambda r: -r["points"])]
    if a["type"] == "aml":
        summary = "AML indicators on this account. " + " ".join(lines) + \
                  " The AML officer reviews the case and decides on any FINTRAC report."
        step = "refer_to_aml_officer"
    else:
        summary = f"Fraud score {a['score']} against an alert threshold of {a['threshold']}. " + " ".join(lines)
        step = "restrict_and_contact_customer" if a["score"] >= 70 else "contact_customer"
    return env("success", "alert summarized", {"summary": summary, "suggested_next_step": step}, [a["snapshot_id"]])


@offline_handler("risk.credit")
def risk_credit(task):
    a = task["alert"]
    stage = a["ews"]["ifrs9_stage_indicator"]
    lines = [f"{r['code']}: {r['detail']}." for r in sorted(a["reasons"], key=lambda r: -r["points"])]
    summary = (f"Early-warning score {a['score']} against a review threshold of {a['threshold']}. " + " ".join(lines) +
               f" IFRS 9 stage indicator {stage} is passed to Finance for the expected credit loss process.")
    step = "collections_referral" if stage == 3 else "limit_review" if stage == 2 else "monitor"
    return env("success", "credit alert summarized", {"summary": summary, "suggested_next_step": step}, [a["snapshot_id"]])
