"""Wealth advisory product: deterministic calculation service + governed advisor workflow.

Design rules (see catalog/domains/wealth-advisory):
- Every number a client could see comes from `calc_snapshot` (tested code), never from a model.
  Model output is checked by `ungrounded_numbers` and routed to human review if it invents one.
- Suitability facts are recomputed here from systems of record at the moment of recommendation;
  facts proposed by a model are ignored.
- Recommendations and orders are ontology actions: firm rules block or route to named approvers,
  and only an advisor can run them. An order can only implement an executed recommendation, with
  the product, side and amount taken from the recorded recommendation.
"""
from __future__ import annotations

import copy
import datetime as dt
import re
from pathlib import Path

import yaml

from ..agents.runtime import Agent
from ..core.db import unj
from ..core.util import REPO_ROOT, digest_obj
from ..ontology.actions import ActionDenied
from ..tools.gateway import CallContext

TENANT, PROJECT = "wealth", "wealth-advisory"
DOMAIN = REPO_ROOT / "catalog" / "domains" / PROJECT
FORECAST_RE = re.compile(r"\b(guarantee[ds]?|will (?:return|earn|grow)|expected return|projected return|risk[- ]free)\b", re.I)
NUM_RE = re.compile(r"(?<![\w-])\$?\d[\d,]*(?:\.\d+)?%?")


# ---------------------------------------------------------------- deterministic calculation service
def calc_snapshot(pos: dict, quotes: dict, kyc: dict, shelf: dict, as_of: str) -> dict:
    """Valuation, weights, drift vs IPS and rebalance amounts. Integer cents; percentages to 1 decimal."""
    prices, products = quotes["prices_cents"], shelf["products"]
    values = {c: units * prices[c] for c, units in sorted(pos["holdings"].items())}
    total = sum(values.values()) + pos["cash_cents"]
    by_class: dict[str, int] = {"cash": pos["cash_cents"]}
    for c, v in values.items():
        k = products[c]["asset_class"]
        by_class[k] = by_class.get(k, 0) + v
    pct = lambda v: round(100.0 * v / total, 1) if total else 0.0          # noqa: E731
    targets = pos["ips"]["targets_pct"]
    drift = {k: round(pct(by_class.get(k, 0)) - t, 1) for k, t in sorted(targets.items())}
    # amount that brings each class back to target, in whole dollars
    rebalance = {k: int(round((t * total / 100.0 - by_class.get(k, 0)) / 100.0)) * 100 for k, t in sorted(targets.items())}
    kyc_age = (dt.date.fromisoformat(as_of) - dt.date.fromisoformat(kyc["kyc_updated_on"])).days
    inputs = {"pos": pos, "quotes": quotes, "kyc": kyc, "shelf": shelf, "as_of": as_of}
    snap = {"snapshot_id": "SNAP-" + digest_obj(inputs).split(":")[-1][:16], "as_of": as_of,
            "account_id": pos["account_id"], "total_cents": total, "position_cents": values,
            "weights_pct": {c: pct(v) for c, v in values.items()},
            "class_cents": dict(sorted(by_class.items())), "class_weights_pct": {k: pct(v) for k, v in sorted(by_class.items())},
            "targets_pct": dict(sorted(targets.items())), "drift_pts": drift, "rebalance_cents": rebalance,
            "kyc": {"risk_tolerance": kyc["risk_tolerance"], "time_horizon": kyc["time_horizon"], "age_days": kyc_age},
            "ips_max_concentration_pct": pos["ips"]["max_concentration_pct"], "shelf": products, "prices_cents": prices}
    snap["figures"] = sorted(_figures(snap))
    return snap


def _figures(snap: dict) -> set[float]:
    """Every number the snapshot can justify, in the units text may use (cents, dollars, percent, days)."""
    out: set[float] = set()

    def add(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return
        out.update({round(float(v), 2), round(abs(float(v)), 2)})

    for key in ("position_cents", "class_cents", "rebalance_cents", "prices_cents"):
        for v in snap[key].values():
            add(v); add(v / 100.0)
    add(snap["total_cents"]); add(snap["total_cents"] / 100.0)
    for key in ("weights_pct", "class_weights_pct", "targets_pct", "drift_pts"):
        for v in snap[key].values():
            add(v)
    add(snap["ips_max_concentration_pct"]); add(snap["kyc"]["risk_tolerance"]); add(snap["kyc"]["age_days"])
    for p in snap["shelf"].values():
        add(p["risk_rating"])
    return out


def ungrounded_numbers(text: str, snap: dict) -> list[str]:
    allowed = set(snap["figures"])
    bad = []
    for m in NUM_RE.findall(text or ""):
        v = round(float(m.strip("$%").replace(",", "")), 2)
        if v not in allowed:
            bad.append(m)
    return bad


def post_trade_concentration(snap: dict, code: str, side: str, amount_cents: int) -> float:
    cur = snap["position_cents"].get(code, 0)
    new = cur + amount_cents if side == "buy" else max(cur - amount_cents, 0)
    total = snap["total_cents"]   # a switch from cash keeps the total unchanged
    return round(100.0 * new / total, 1) if total else 0.0


def facts_for(snap: dict, proposal: dict, conflict_disclosed: bool = False) -> dict:
    p = snap["shelf"].get(proposal["product_code"])
    if p is None:
        raise ActionDenied(f"{proposal['product_code']} is not on the product shelf")
    return {"product_risk": p["risk_rating"], "client_risk_tolerance": snap["kyc"]["risk_tolerance"],
            "kyc_age_days": snap["kyc"]["age_days"], "product_approved": bool(p["kyp_reviewed"]),
            "post_trade_concentration_pct": post_trade_concentration(snap, proposal["product_code"], proposal["side"],
                                                                     proposal["amount_cents"]),
            "ips_max_concentration_pct": float(snap["ips_max_concentration_pct"]),
            "conflict_of_interest": bool(p["proprietary"]), "conflict_disclosed": bool(conflict_disclosed)}


# ---------------------------------------------------------------- advisor workflow
class WealthAdvisoryApp:
    def __init__(self, f, env: str = "nonprod"):
        self.f, self.env = f, env
        self.cfg = yaml.safe_load((DOMAIN / "project.yaml").read_text(encoding="utf-8"))
        self.copilot = self._agent("advisor-copilot")
        self.reviewer = self._agent("suitability-reviewer")

    def _agent(self, aid: str) -> Agent:
        row = self.f.db.one("SELECT manifest FROM agents WHERE id=?", (aid,))
        if not row:
            raise RuntimeError(f"agent {aid} is not registered; run bootstrap")
        return Agent(self.f, copy.deepcopy(unj(row["manifest"])))

    def _advisor(self, user: str):
        p = self.f.directory.get(user)
        if not (p.kind == "human" and p.has_role("advisor") and p.in_tenant(TENANT)):
            raise PermissionError(f"{user} is not an advisor in tenant {TENANT}")
        return p

    def snapshot(self, account_id: str, extra_codes: tuple[str, ...] = (), run_id: str = "adhoc") -> dict:
        ctx = CallContext(self.copilot.principal, TENANT, PROJECT, run_id, self.env, purpose="product")
        t = self.f.tools
        pos = t.invoke(ctx, "portfolio", "get_holdings", {"account_id": account_id})
        kyc = t.invoke(ctx, "kyc", "get", {"account_id": account_id})
        codes = sorted(set(pos["holdings"]) | set(extra_codes) | set(self._shelf_codes()))
        quotes = t.invoke(ctx, "market_data", "quotes", {"codes": codes})
        shelf = t.invoke(ctx, "product_shelf", "get", {"codes": codes})
        snap = calc_snapshot(pos, quotes, kyc, shelf, quotes["as_of"])
        self.f.ledger.record("wealth.snapshot", snap["snapshot_id"], "wealth-calc",
                             {"account": account_id, "as_of": snap["as_of"], "inputs_digest": digest_obj([pos, quotes, kyc, shelf])},
                             PROJECT)
        return snap

    def _shelf_codes(self) -> list[str]:
        book = yaml.safe_load((REPO_ROOT / "samples" / "wealth" / "book.yaml").read_text(encoding="utf-8"))
        return sorted(book["products"])   # synthetic shelf listing; production: product_shelf list endpoint

    def prepare_review(self, advisor: str, account_id: str) -> dict:
        self._advisor(advisor)
        snap = self.snapshot(account_id)
        r = self.copilot.run(None, {"kind": "wealth.review", "snapshot": snap}, tenant=TENANT, env=self.env,
                             purpose="product.advisory", data_class="confidential")
        if r.status != "success":
            return {"status": r.status, "snapshot_id": snap["snapshot_id"], "issues": [r.summary]}
        text = r.payload["review_notes"] + " " + " ".join(p["basis"] for p in r.payload["proposals"])
        issues = [f"number not in snapshot: {n}" for n in ungrounded_numbers(text, snap)]
        issues += [f"forecast or promise language: '{m}'" for m in FORECAST_RE.findall(text)]
        for p in r.payload["proposals"]:
            if p["amount_cents"] not in {abs(v) for v in snap["rebalance_cents"].values()} | {0}:
                issues.append(f"proposal amount {p['amount_cents']} not from the calculation service")
        if r.payload["snapshot_id"] != snap["snapshot_id"]:
            issues.append("review cites a different snapshot")
        status = "needs_review" if issues else "ready_for_advisor"
        self.f.ledger.record("wealth.review.prepared", account_id, advisor,
                             {"snapshot": snap["snapshot_id"], "status": status, "issues": issues,
                              "agent_evidence": r.evidence_refs[-1]}, PROJECT)
        return {"status": status, "snapshot_id": snap["snapshot_id"], "review_notes": r.payload["review_notes"],
                "proposals": r.payload["proposals"], "issues": issues, "snapshot": snap}

    def suitability(self, advisor: str, account_id: str, proposal: dict, conflict_disclosed: bool = False) -> dict:
        self._advisor(advisor)
        snap = self.snapshot(account_id, (proposal["product_code"],))
        facts = facts_for(snap, proposal, conflict_disclosed)
        r = self.reviewer.run(None, {"kind": "wealth.suitability", "facts": facts, "proposal": proposal,
                                     "snapshot_id": snap["snapshot_id"]},
                              tenant=TENANT, env=self.env, purpose="product.suitability", data_class="confidential")
        return {"facts": facts, "snapshot_id": snap["snapshot_id"], **r.payload}

    def recommend(self, advisor: str, account_id: str, proposal: dict, rationale: str,
                  conflict_disclosed: bool = False, request_id: str | None = None) -> dict:
        """Run the ProposeRecommendation ontology action with facts recomputed from systems of record."""
        self._advisor(advisor)
        if request_id:   # resume after approvals: identical params are required by the approval binding
            req = self.f.actions.request(request_id)
            return self.f.actions.execute("ProposeRecommendation", advisor, req["subject"], req["params"],
                                          env=self.env, tenant=TENANT, request_id=request_id)
        snap = self.snapshot(account_id, (proposal["product_code"],))
        params = {"product_code": proposal["product_code"], "side": proposal["side"],
                  "amount_cents": int(proposal["amount_cents"]), "rationale": rationale,
                  "snapshot_id": snap["snapshot_id"], **facts_for(snap, proposal, conflict_disclosed)}
        return self.f.actions.execute("ProposeRecommendation", advisor, {"id": account_id, "tenant": TENANT}, params,
                                      env=self.env, tenant=TENANT)

    def order_ticket(self, advisor: str, recommendation_request_id: str) -> dict:
        """The exact order an executed recommendation implies, plus the binding the advisor confirms.
        Product, side and amount come from the recorded recommendation, never from the caller."""
        self._advisor(advisor)
        rec = self.f.actions.request(recommendation_request_id)
        if rec["action"] != "ProposeRecommendation" or rec["status"] != "executed":
            raise ActionDenied(f"{recommendation_request_id} is not an executed recommendation")
        if rec["actor"] != advisor:
            raise ActionDenied("only the advisor of record can implement this recommendation")
        rec_id = rec["result"]["recommendation_id"]
        if self.f.db.one("SELECT order_id FROM orders_sor WHERE recommendation_id=?", (rec_id,)):
            raise ActionDenied(f"recommendation {rec_id} has already been implemented by an order")
        p = rec["params"]
        params = {"recommendation_id": rec_id, "product_code": p["product_code"],
                  "side": p["side"], "amount_cents": p["amount_cents"]}
        return {"subject": rec["subject"], "params": params,
                "binding": self.f.actions.tool_binding("PlaceTradeOrder", rec["subject"], params, self.env, TENANT)}

    def place_order(self, advisor: str, recommendation_request_id: str, confirmations: list[str]) -> dict:
        """Send the order. `confirmations` = the advisor's approval of the ticket binding (client instruction confirmed)."""
        t = self.order_ticket(advisor, recommendation_request_id)
        return self.f.actions.execute("PlaceTradeOrder", advisor, t["subject"], t["params"], env=self.env, tenant=TENANT,
                                      tool_approvals=confirmations)


# ---------------------------------------------------------------- evaluation (Pipeline C for the product agents)
def evaluate(f, app: WealthAdvisoryApp) -> dict:
    suite = yaml.safe_load((DOMAIN / "evals.yaml").read_text(encoding="utf-8"))
    results: dict[str, list[bool]] = {}
    for case in suite["cases"]:
        ok = False
        if case["kind"] == "review":
            out = app.prepare_review(case["advisor"], case["account"])
            ok = out["status"] == case["expect"]["status"] and len(out["proposals"]) >= case["expect"].get("min_proposals", 0)
        elif case["kind"] == "suitability":
            out = app.suitability(case["advisor"], case["account"], case["proposal"])
            ok = out["suggested_route"] == case["expect"]["route"]
        elif case["kind"] == "grounding":
            ok = bool(ungrounded_numbers(case["text"], app.snapshot(case["account"]))) == case["expect"]["ungrounded"]
        elif case["kind"] == "forecast":
            ok = bool(FORECAST_RE.search(case["text"])) == case["expect"]["flagged"]
        results.setdefault(case["category"], []).append(ok)
    scores = {k: round(sum(v) / len(v), 3) for k, v in results.items()}
    critical = [k for k in suite["critical_categories"] if scores.get(k, 0.0) < 1.0]
    avg = round(sum(scores.values()) / len(scores), 3)
    gate = "pass" if not critical and avg >= suite["min_average"] else "fail"
    f.ledger.record("eval.wealth", PROJECT, "eval-harness", {"scores": scores, "critical_failures": critical, "gate": gate},
                    PROJECT)
    return {"scores": scores, "average": avg, "critical_failures": critical, "gate": gate}
