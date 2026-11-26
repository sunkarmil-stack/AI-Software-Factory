"""Retail account risk product: transaction monitoring (fraud + AML indicators) and credit early warning.

Design rules (see catalog/domains/retail-account-risk):
- Scores are transparent, rule-based and config-driven (scoring.yaml). Every point carries a reason code,
  so every alert and decision is explainable. The scoring configuration is a model for model-risk purposes.
- Monitoring reads systems of record as the `risk-monitor` workload. Agents summarize evidence only;
  numbers they cite must come from the computed features, and they may not use accusatory language.
- AML indicators (structuring, the FINTRAC 24-hour large-cash threshold) never trigger automatic reports:
  the case goes to the AML officer, who decides on any FINTRAC filing.
- Opening cases, restricting accounts and reducing limits are ontology actions with named approvers.
"""
from __future__ import annotations

import copy
import datetime as dt
import re

import yaml

from ..agents.runtime import Agent
from ..core.db import unj
from ..core.util import REPO_ROOT, digest_obj, new_id
from ..ontology.actions import ActionDenied
from ..tools.gateway import CallContext

TENANT, PROJECT = "retail", "retail-account-risk"
DOMAIN = REPO_ROOT / "catalog" / "domains" / PROJECT
NUM_RE = re.compile(r"(?<![\w-])\$?\d[\d,]*(?:\.\d+)?%?")
ACCUSATORY_RE = re.compile(r"\b(fraudster|committed fraud|is laundering|criminal|guilty|stole)\b", re.I)


def scoring() -> dict:
    return yaml.safe_load((DOMAIN / "scoring.yaml").read_text())


def _ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---------------------------------------------------------------- deterministic scoring
def fraud_features(data: dict, cfg: dict) -> tuple[int, list[dict]]:
    p, txns, fc = data["profile"], sorted(data["transactions"], key=lambda t: t["ts"]), cfg["fraud"]
    reasons = []

    def add(code, detail):
        reasons.append({"code": code, "points": fc["weights"][code], "detail": detail})
    win = fc["velocity"]["window_minutes"]
    best = 0
    for i, t in enumerate(txns):
        n = sum(1 for u in txns[i:] if (_ts(u["ts"]) - _ts(t["ts"])).total_seconds() <= win * 60)
        best = max(best, n)
    if best >= fc["velocity"]["count"]:
        add("VELOCITY", f"{best} transactions within {win} minutes")
    top = max((t["amount_cents"] for t in txns), default=0)
    ratio = round(top / p["avg_txn_cents_90d"], 1) if p["avg_txn_cents_90d"] else 0.0
    if ratio >= fc["amount_spike_ratio"]:
        add("AMOUNT_SPIKE", f"largest amount {ratio} times the 90-day average")
    new_dev = sorted({t["device"] for t in txns if t["channel"] != "cash_deposit"} - set(p["known_devices"]))
    if new_dev:
        add("NEW_DEVICE", f"{len(new_dev)} unrecognized device(s)")
    for a, b in zip(txns, txns[1:]):
        mins = (_ts(b["ts"]) - _ts(a["ts"])).total_seconds() / 60
        if a["country"] != b["country"] and mins <= fc["geo_window_minutes"]:
            add("GEO_IMPOSSIBLE", f"{a['country']} to {b['country']} within {int(mins)} minutes")
            break
    new_pay = sorted({t["payee"] for t in txns if t["channel"] == "etransfer"} - set(p["known_payees"]))
    if new_pay:
        add("NEW_PAYEE_TRANSFER", f"transfers to {len(new_pay)} new payee(s)")
    cnp = [t for t in txns if t["channel"] == "card_not_present" and t["mcc"] in fc["high_risk_mcc"]]
    if cnp:
        add("CNP_HIGH_RISK_MCC", f"{len(cnp)} card-not-present purchase(s) in high-risk categories")
    return min(100, sum(r["points"] for r in reasons)), reasons


def aml_indicators(data: dict, cfg: dict) -> dict:
    ac = cfg["aml"]
    cash = sorted((t for t in data["transactions"] if t["channel"] in ac["cash_channels"]), key=lambda t: t["ts"])
    window = dt.timedelta(hours=ac["window_hours"])
    best_total, best_band = 0, 0
    lo, hi = ac["structuring_band_cents"]
    for i, t in enumerate(cash):
        inwin = [u for u in cash[i:] if _ts(u["ts"]) - _ts(t["ts"]) <= window]
        best_total = max(best_total, sum(u["amount_cents"] for u in inwin))
        best_band = max(best_band, sum(1 for u in inwin if lo <= u["amount_cents"] <= hi))
    reasons = []
    lctr = best_total >= ac["lctr_threshold_cents"]
    if lctr:
        reasons.append({"code": "CASH_24H_THRESHOLD", "points": 0,
                        "detail": f"cash deposits of ${best_total / 100:,.2f} within {ac['window_hours']} hours"})
    structuring = best_band >= ac["structuring_min_count"]
    if structuring:
        reasons.append({"code": "STRUCTURING_PATTERN", "points": 0,
                        "detail": f"{best_band} cash deposits just below $10,000 within {ac['window_hours']} hours"})
    return {"lctr_threshold_met": lctr, "suspected_structuring": structuring, "cash_24h_cents": best_total,
            "reasons": reasons}


def credit_ews(credit: dict, cfg: dict) -> dict:
    cc = cfg["credit"]
    pts, reasons = cc["points"], []
    if credit["facility"] == "none":
        return {"score": 0, "reasons": [], "ifrs9_stage_indicator": None, "utilization_pct": 0.0}
    util = round(100.0 * credit["balance_cents"] / credit["limit_cents"], 1) if credit["limit_cents"] else 0.0
    drop = credit["bureau_score_at_origination"] - credit["bureau_score"]
    dpd = credit["days_past_due"]
    if dpd > 30:
        reasons.append({"code": "DPD_30", "points": pts["DPD_30"], "detail": f"{dpd} days past due"})
    if dpd > 90:
        reasons.append({"code": "DPD_90", "points": pts["DPD_90"], "detail": f"{dpd} days past due (over 90)"})
    if util >= cc["utilization_pct"]:
        reasons.append({"code": "HIGH_UTILIZATION", "points": pts["HIGH_UTILIZATION"], "detail": f"{util}% of limit used"})
    if drop >= cc["score_drop"]:
        reasons.append({"code": "SCORE_DROP", "points": pts["SCORE_DROP"], "detail": f"bureau score down {drop} points since origination"})
    if credit["missed_payments_6m"] >= cc["missed_payments"]:
        reasons.append({"code": "MISSED_PAYMENTS", "points": pts["MISSED_PAYMENTS"],
                        "detail": f"{credit['missed_payments_6m']} missed payments in 6 months"})
    if credit["nsf_6m"] >= cc["nsf"]:
        reasons.append({"code": "NSF", "points": pts["NSF"], "detail": f"{credit['nsf_6m']} NSF events in 6 months"})
    i9 = cc["ifrs9"]
    stage = 3 if dpd > i9["stage3_dpd_over"] else 2 if (dpd > i9["stage2_dpd_over"] or drop >= i9["sicr_score_drop"]) else 1
    return {"score": min(100, sum(r["points"] for r in reasons)), "reasons": reasons, "ifrs9_stage_indicator": stage,
            "utilization_pct": util, "score_drop": drop}


def figures_of(alert: dict) -> set[float]:
    out: set[float] = set()
    for r in alert.get("reasons", []):
        for m in NUM_RE.findall(r["detail"]):
            out.add(round(float(m.strip("$%").replace(",", "")), 1))
    for k in ("score", "threshold"):
        if isinstance(alert.get(k), (int, float)):
            out.add(round(float(alert[k]), 1))
    return out


def ungrounded(text: str, alert: dict) -> list[str]:
    allowed = figures_of(alert)
    return [m for m in NUM_RE.findall(text or "") if round(float(m.strip("$%").replace(",", "")), 1) not in allowed]


# ---------------------------------------------------------------- workflow
class RetailRiskApp:
    def __init__(self, f, env: str = "nonprod"):
        self.f, self.env, self.cfg = f, env, scoring()
        self.alerts: dict[str, dict] = {}
        self.investigator = self._agent("fraud-investigator")
        self.credit_analyst_agent = self._agent("credit-watch-analyst")

    def _agent(self, aid):
        row = self.f.db.one("SELECT manifest FROM agents WHERE id=?", (aid,))
        if not row:
            raise RuntimeError(f"agent {aid} is not registered; run bootstrap")
        return Agent(self.f, copy.deepcopy(unj(row["manifest"])))

    def _role(self, user, *roles):
        p = self.f.directory.get(user)
        if not (p.kind == "human" and any(p.has_role(r) for r in roles) and p.in_tenant(TENANT)):
            raise PermissionError(f"{user} does not hold {' or '.join(roles)} in tenant {TENANT}")
        return p

    def _ctx(self, run_id):
        return CallContext(self.f.directory.get("risk-monitor"), TENANT, PROJECT, run_id, self.env, purpose="product")

    # ---- 1. monitoring (deterministic, as the risk-monitor workload)
    def monitor(self, account_ids: list[str]) -> list[dict]:
        out = []
        for acc in account_ids:
            run = new_id("MON")
            data = self.f.tools.invoke(self._ctx(run), "transactions", "recent", {"account_id": acc})
            credit = self.f.tools.invoke(self._ctx(run), "credit_exposure", "get", {"account_id": acc})["credit"]
            snap = "SNAP-" + digest_obj([data, credit, self.cfg["version"]]).split(":")[-1][:16]
            fscore, freasons = fraud_features(data, self.cfg)
            aml = aml_indicators(data, self.cfg)
            ews = credit_ews(credit, self.cfg)
            base = {"account_id": acc, "snapshot_id": snap, "scoring_version": self.cfg["version"],
                    "vulnerable_customer": data["vulnerable_customer"], "credit": credit}
            if fscore >= self.cfg["fraud"]["alert_threshold"]:
                out.append(self._alert({**base, "type": "fraud", "score": fscore, "reasons": freasons,
                                        "threshold": self.cfg["fraud"]["alert_threshold"]}))
            if aml["lctr_threshold_met"] or aml["suspected_structuring"]:
                out.append(self._alert({**base, "type": "aml", "score": 0, "reasons": aml["reasons"], "aml": aml}))
            if ews["score"] >= self.cfg["credit"]["review_threshold"]:
                out.append(self._alert({**base, "type": "credit", "score": ews["score"], "reasons": ews["reasons"],
                                        "ews": ews, "threshold": self.cfg["credit"]["review_threshold"]}))
            self.f.ledger.record("risk.monitored", acc, "risk-monitor",
                                 {"snapshot": snap, "fraud_score": fscore, "ews_score": ews["score"],
                                  "aml": {k: aml[k] for k in ("lctr_threshold_met", "suspected_structuring")}}, PROJECT)
        return out

    def _alert(self, a):
        a["alert_id"] = new_id("ALRT")
        self.alerts[a["alert_id"]] = a
        return a

    # ---- 2. investigation summary (agent drafts; checks decide whether it is usable)
    def investigate(self, analyst: str, alert_id: str) -> dict:
        self._role(analyst, "fraud_analyst", "credit_analyst", "aml_officer")
        a = self.alerts[alert_id]
        agent, purpose = (self.credit_analyst_agent, "product.risk.credit") if a["type"] == "credit" else \
            (self.investigator, "product.risk.fraud")
        r = agent.run(None, {"kind": f"risk.{'credit' if a['type'] == 'credit' else 'investigate'}", "alert": a},
                      tenant=TENANT, env=self.env, purpose=purpose, data_class="confidential")
        summary = r.payload.get("summary", "") if r.status == "success" else ""
        issues = [f"number not in the evidence: {n}" for n in ungrounded(summary, a)]
        issues += [f"accusatory language: '{m}'" for m in ACCUSATORY_RE.findall(summary)]
        a["investigation"] = {"summary": "" if issues else summary, "suggested_next_step": r.payload.get("suggested_next_step"),
                              "issues": issues, "status": "needs_review" if issues else "ready_for_analyst"}
        return a["investigation"]

    # ---- 3. ontology actions
    def open_case(self, analyst: str, alert_id: str, request_id: str | None = None) -> dict:
        a = self.alerts[alert_id]
        self._role(analyst, "credit_analyst" if a["type"] == "credit" else "fraud_analyst")
        if request_id:
            req = self.f.actions.request(request_id)
            return self.f.actions.execute("OpenRiskCase", analyst, req["subject"], req["params"], env=self.env,
                                          tenant=TENANT, request_id=request_id)
        aml = a.get("aml") or aml_indicators({"transactions": []}, self.cfg)
        params = {"case_type": a["type"], "alert_id": a["alert_id"], "score": float(a["score"]),
                  "reason_codes": ",".join(r["code"] for r in a["reasons"]), "reason_code_count": len(a["reasons"]),
                  "snapshot_id": a["snapshot_id"], "suspected_money_laundering": bool(aml["suspected_structuring"]),
                  "lctr_threshold_met": bool(aml["lctr_threshold_met"])}
        out = self.f.actions.execute("OpenRiskCase", analyst, {"id": a["account_id"], "tenant": TENANT}, params,
                                     env=self.env, tenant=TENANT)
        a["case_request"] = out.get("request_id")
        return out

    def _case(self, case_request_id: str, case_type: str) -> tuple[dict, str]:
        req = self.f.actions.request(case_request_id)
        if req["action"] != "OpenRiskCase" or req["status"] != "executed" or req["params"]["case_type"] != case_type:
            raise ActionDenied(f"{case_request_id} is not an executed {case_type} case")
        return req, req["result"]["case_id"]

    def restriction_ticket(self, analyst: str, case_request_id: str, restriction: str = "outgoing_payments",
                           duration_hours: int = 48) -> dict:
        self._role(analyst, "fraud_analyst")
        req, case_id = self._case(case_request_id, "fraud")
        a = self.alerts[req["params"]["alert_id"]]
        if self.f.db.one("SELECT restriction_id FROM restrictions_sor WHERE case_id=? AND status='active'", (case_id,)):
            raise ActionDenied(f"case {case_id} already has an active restriction")
        params = {"case_id": case_id, "restriction": restriction, "fraud_score": float(a["score"]),
                  "restrict_threshold": float(self.cfg["fraud"]["restrict_threshold"]), "duration_hours": int(duration_hours),
                  "vulnerable_customer": bool(a["vulnerable_customer"])}
        return {"subject": req["subject"], "params": params,
                "binding": self.f.actions.tool_binding("RestrictAccount", req["subject"], params, self.env, TENANT)}

    def restrict(self, analyst: str, ticket: dict, confirmations: list[str], request_id: str | None = None) -> dict:
        self._role(analyst, "fraud_analyst")
        return self.f.actions.execute("RestrictAccount", analyst, ticket["subject"], ticket["params"], env=self.env,
                                      tenant=TENANT, tool_approvals=confirmations, request_id=request_id)

    def limit_ticket(self, analyst: str, case_request_id: str, new_limit_cents: int, notice_days: int) -> dict:
        self._role(analyst, "credit_analyst")
        req, case_id = self._case(case_request_id, "credit")
        a = self.alerts[req["params"]["alert_id"]]
        c = a["credit"]
        params = {"case_id": case_id, "current_limit_cents": int(c["limit_cents"]), "new_limit_cents": int(new_limit_cents),
                  "balance_cents": int(c["balance_cents"]), "ews_score": float(a["score"]),
                  "review_threshold": float(self.cfg["credit"]["review_threshold"]), "notice_days": int(notice_days),
                  "policy_notice_days": int(self.cfg["credit"]["policy_notice_days"])}
        return {"subject": req["subject"], "params": params,
                "binding": self.f.actions.tool_binding("ReduceCreditLimit", req["subject"], params, self.env, TENANT)}

    def reduce_limit(self, analyst: str, ticket: dict, approvals: list[str]) -> dict:
        self._role(analyst, "credit_analyst")
        return self.f.actions.execute("ReduceCreditLimit", analyst, ticket["subject"], ticket["params"], env=self.env,
                                      tenant=TENANT, tool_approvals=approvals)


# ---------------------------------------------------------------- evaluation (Pipeline C for the product agents)
def evaluate(f, app: RetailRiskApp) -> dict:
    suite = yaml.safe_load((DOMAIN / "evals.yaml").read_text())
    results: dict[str, list[bool]] = {}
    alerts = {(a["account_id"], a["type"]): a for a in app.monitor(["acc-100", "acc-101", "acc-102", "acc-103"])}
    for case in suite["cases"]:
        ok = False
        if case["kind"] == "alerting":
            a = alerts.get((case["account"], case["type"]))
            ok = (a is not None) == case["expect_alert"] and (a is None or a["score"] == case.get("expect_score", a["score"]))
        elif case["kind"] == "stage":
            a = alerts.get((case["account"], "credit"))
            ok = a is not None and a["ews"]["ifrs9_stage_indicator"] == case["expect_stage"]
        elif case["kind"] == "investigation":
            inv = app.investigate(case["analyst"], alerts[(case["account"], case["type"])]["alert_id"])
            ok = inv["status"] == "ready_for_analyst" and inv["suggested_next_step"] == case["expect_next_step"]
        elif case["kind"] == "grounding":
            ok = bool(ungrounded(case["text"], alerts[(case["account"], case["type"])])) == case["expect_ungrounded"]
        elif case["kind"] == "fair_language":
            ok = bool(ACCUSATORY_RE.search(case["text"])) == case["expect_flagged"]
        results.setdefault(case["category"], []).append(ok)
    scores = {k: round(sum(v) / len(v), 3) for k, v in results.items()}
    critical = [k for k in suite["critical_categories"] if scores.get(k, 0.0) < 1.0]
    avg = round(sum(scores.values()) / len(scores), 3)
    gate = "pass" if not critical and avg >= suite["min_average"] else "fail"
    f.ledger.record("eval.retailrisk", PROJECT, "eval-harness", {"scores": scores, "critical_failures": critical,
                                                                 "gate": gate}, PROJECT)
    return {"scores": scores, "average": avg, "critical_failures": critical, "gate": gate}
