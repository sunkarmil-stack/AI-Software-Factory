"""Wealth-advisory product built on the factory: domain pack, governed suitability rules, advisor workflow,
order controls, grounding of numbers, isolation and the product evaluation gate."""
import pytest

from factory.demo import run_delivery, run_release, run_spec
from factory.demo_wealth import approve_pack, load_spec
from factory.domains import load_domain
from factory.models.gateway import OFFLINE_HANDLERS, ModelContext, ModelDenied
from factory.ontology.actions import ActionDenied
from factory.product.wealth import (WealthAdvisoryApp, calc_snapshot, evaluate, post_trade_concentration,
                                    ungrounded_numbers)
from factory.tools.gateway import CallContext, ToolDenied

BOND = {"product_code": "CDN-BOND-AGG", "side": "buy", "amount_cents": 1130900}
RATIONALE = "Restores fixed income toward the IPS target; product risk 2 is within the client's tolerance."


@pytest.fixture
def wf(f):
    load_domain(f, "wealth-advisory")
    approve_pack(f)
    return f


@pytest.fixture
def app(wf):
    return WealthAdvisoryApp(wf)


# ---------------------------------------------------------------- domain pack and governance
def test_domain_pack_onboards_and_nothing_applies_before_approval(f):
    out = load_domain(f, "wealth-advisory")
    assert f.project("wealth-advisory")["tenant"] == "wealth" and f.project("wealth-advisory")["risk_tier"] == 3
    assert not out["invalid"] and len(out["proposals"]) == 16
    roles = {p["kind"]: p["roles"] for p in out["proposals"]}
    assert roles["add_rule"] == ["ontology_owner", "product_owner"]
    order = next(p for p in out["proposals"] if p["kind"] == "add_action" and p["payload"]["action"]["name"] == "PlaceTradeOrder")
    assert "security" in order["roles"]                                   # consequential action needs security
    assert "ProposeRecommendation" not in f.ontology.all_actions()
    with pytest.raises(ActionDenied, match="does not exist"):
        f.actions.execute("ProposeRecommendation", "adv.ada", {"id": "ACC-W-1001", "tenant": "wealth"}, {})
    assert load_domain(f, "wealth-advisory")["proposals"] == []            # idempotent re-load
    approve_pack(f)
    assert not f.ontology.proposals("pending") and "PlaceTradeOrder" in f.ontology.all_actions()


# ---------------------------------------------------------------- deterministic calculation
def test_calculation_service_is_deterministic_and_exact(app):
    s1, s2 = app.snapshot("ACC-W-1001"), app.snapshot("ACC-W-1001")
    assert s1["snapshot_id"] == s2["snapshot_id"] and s1["total_cents"] == 8014000
    assert s1["class_weights_pct"] == {"cash": 15.0, "equity": 64.1, "fixed_income": 20.9}
    assert s1["drift_pts"]["fixed_income"] == -14.1 and s1["rebalance_cents"]["fixed_income"] == 1130900
    assert s1["kyc"]["age_days"] == 120
    assert post_trade_concentration(s1, "CDN-BOND-AGG", "buy", 1130900) == 35.0
    assert ungrounded_numbers("Market value $80,140.00; equity 64.1%.", s1) == []
    assert ungrounded_numbers("It yields 4.75% on $81,000.", s1) == ["4.75%", "$81,000"]


# ---------------------------------------------------------------- suitability rules and approvals
def test_suitable_recommendation_executes_and_is_recorded(app, wf):
    r = app.recommend("adv.ada", "ACC-W-1001", BOND, RATIONALE)
    assert r["status"] == "executed" and r["result"]["recommendation_id"].startswith("REC-")
    row = wf.db.one("SELECT * FROM advice_records_sor WHERE recommendation_id=?", (r["result"]["recommendation_id"],))
    assert row["rationale"] == RATIONALE and row["snapshot_id"].startswith("SNAP-")      # books and records
    assert {x["rule"] for x in r["rule_results"]} >= {"R-RECOMMENDATION-KYC-CURRENT", "R-RECOMMENDATION-RISK-FIT"}


@pytest.mark.parametrize("proposal,disclosed,status,detail", [
    ({"product_code": "TECH-THEME", "side": "buy", "amount_cents": 500000}, False, "pending_approval", ["supervisor"]),
    ({"product_code": "CDN-EQ-IDX", "side": "buy", "amount_cents": 1000000}, False, "pending_approval", ["portfolio_manager"]),
    ({"product_code": "PROP-BAL-FUND", "side": "buy", "amount_cents": 100000}, True, "pending_approval", ["compliance_officer"]),
    ({"product_code": "PROP-BAL-FUND", "side": "buy", "amount_cents": 100000}, False, "blocked", "not been disclosed"),
    ({"product_code": "CRYPTO-ETF", "side": "buy", "amount_cents": 100000}, False, "blocked", "know-your-product"),
])
def test_rules_block_or_route_to_named_approvers(app, proposal, disclosed, status, detail):
    r = app.recommend("adv.ada", "ACC-W-1001", proposal, RATIONALE, conflict_disclosed=disclosed)
    assert r["status"] == status
    if status == "pending_approval":
        assert r["roles"] == detail
    else:
        assert any(detail in m for m in r["messages"])


def test_stale_kyc_blocks_any_recommendation(app):
    r = app.recommend("adv.ben", "ACC-W-1002", {"product_code": "CDN-BOND-AGG", "side": "buy", "amount_cents": 100000},
                      RATIONALE)
    assert r["status"] == "blocked" and "KYC" in r["messages"][0]


def test_approval_is_bound_and_separated(app, wf):
    tech = {"product_code": "TECH-THEME", "side": "buy", "amount_cents": 500000}
    r = app.recommend("adv.ada", "ACC-W-1001", tech, RATIONALE)
    with pytest.raises(Exception):
        wf.actions.approve(r["request_id"], "adv.ada", "supervisor")      # advisor holds no supervisor role
    wf.actions.approve(r["request_id"], "sup.sid", "supervisor")
    req = wf.actions.request(r["request_id"])
    tampered = {**req["params"], "amount_cents": 5000000}
    t = wf.actions.execute("ProposeRecommendation", "adv.ada", req["subject"], tampered, tenant="wealth",
                           request_id=r["request_id"])
    assert t["status"] == "pending_approval"                                # approval does not cover a bigger trade
    ok = app.recommend("adv.ada", "ACC-W-1001", tech, "", request_id=r["request_id"])
    assert ok["status"] == "executed"


def test_only_advisors_run_actions_and_agents_cannot(app, wf):
    with pytest.raises(PermissionError):
        app.recommend("sup.sid", "ACC-W-1001", BOND, RATIONALE)
    params = {"product_code": "CDN-BOND-AGG", "side": "buy", "amount_cents": 1, "rationale": RATIONALE, "snapshot_id": "S",
              "product_risk": 2, "client_risk_tolerance": 3, "kyc_age_days": 1, "product_approved": True,
              "post_trade_concentration_pct": 1.0, "ips_max_concentration_pct": 40.0, "conflict_of_interest": False,
              "conflict_disclosed": False}
    with pytest.raises(ActionDenied, match="no grant"):
        wf.actions.execute("ProposeRecommendation", "agent:advisor-copilot", {"id": "ACC-W-1001", "tenant": "wealth"},
                           params, tenant="wealth")
    ctx = CallContext(wf.directory.get("agent:advisor-copilot"), "wealth", "wealth-advisory", "r", "nonprod", purpose="product")
    with pytest.raises(ToolDenied):
        wf.tools.invoke(ctx, "orders", "submit", {"subject_id": "ACC-W-1001", "recommendation_id": "x", "product_code": "A",
                                                   "side": "buy", "amount_cents": 1, "tenant": "wealth"}, op_id="o1")


# ---------------------------------------------------------------- orders
def test_order_needs_advisor_confirmation_and_matches_recommendation(app, wf):
    rec = app.recommend("adv.ada", "ACC-W-1001", BOND, RATIONALE)
    with pytest.raises(ActionDenied, match="advisor"):
        app.place_order("adv.ada", rec["request_id"], [])                  # tier 3: no confirmation, no order
    with pytest.raises(ActionDenied, match="advisor of record"):
        app.order_ticket("adv.ben", rec["request_id"])
    t = app.order_ticket("adv.ada", rec["request_id"])
    assert t["params"]["amount_cents"] == BOND["amount_cents"]              # from the record, not the caller
    conf = wf.approvals.approve("adv.ada", "advisor", t["binding"], rationale="client instruction confirmed")
    o = app.place_order("adv.ada", rec["request_id"], [conf])
    assert o["status"] == "executed" and o["result"]["status"] == "accepted"
    with pytest.raises(ActionDenied, match="already been implemented"):
        app.order_ticket("adv.ada", rec["request_id"])                     # one order per recommendation
    assert wf.ledger.query(kind="ontology.action.executed")


def test_order_requires_executed_recommendation(app, wf):
    r = app.recommend("adv.ada", "ACC-W-1001", {"product_code": "TECH-THEME", "side": "buy", "amount_cents": 500000}, RATIONALE)
    with pytest.raises(ActionDenied, match="not an executed recommendation"):
        app.order_ticket("adv.ada", r["request_id"])


# ---------------------------------------------------------------- product agents: grounding, data class, isolation
def test_review_is_grounded_and_invented_numbers_go_to_human_review(app, monkeypatch):
    ok = app.prepare_review("adv.ada", "ACC-W-1001")
    assert ok["status"] == "ready_for_advisor" and ok["issues"] == []
    assert {(p["product_code"], p["side"]) for p in ok["proposals"]} == {("CDN-BOND-AGG", "buy"), ("CDN-EQ-IDX", "sell")}
    real = OFFLINE_HANDLERS["wealth.review"]

    def hallucinating(task):
        out = real(task)
        out["payload"]["review_notes"] += " The bond fund yields 4.75% and will return more than cash."
        out["payload"]["proposals"][0]["amount_cents"] = 999999
        return out
    monkeypatch.setitem(OFFLINE_HANDLERS, "wealth.review", hallucinating)
    bad = app.prepare_review("adv.ada", "ACC-W-1001")
    assert bad["status"] == "needs_review"
    assert any("4.75%" in i for i in bad["issues"]) and any("will return" in i for i in bad["issues"])
    assert any("999999" in i for i in bad["issues"])


def test_restricted_data_is_not_approved_for_the_advisory_model(wf):
    ctx = ModelContext(wf.directory.get("agent:advisor-copilot"), "wealth-advisory", "wealth", "r", "r",
                       "product.advisory", "restricted")
    with pytest.raises(ModelDenied):
        wf.models.complete(ctx, "advisory-copilot", "sys", {"kind": "wealth.review"})


def test_tenant_isolation_for_wealth_tools(wf):
    retail_agent = wf.directory.get("agent:rag-assistant")
    ctx = CallContext(retail_agent, "retail", "accounts", "r", "nonprod", purpose="product")
    with pytest.raises(ToolDenied):
        wf.tools.invoke(ctx, "portfolio", "get_holdings", {"account_id": "ACC-W-1001"})
    with pytest.raises(PermissionError):
        WealthAdvisoryApp(wf).prepare_review("po.priya", "ACC-W-1001")


# ---------------------------------------------------------------- evaluation gate and delivery
def test_product_evaluation_gate(app, wf, monkeypatch):
    ev = evaluate(wf, app)
    assert ev["gate"] == "pass" and ev["critical_failures"] == []
    real = OFFLINE_HANDLERS["wealth.suitability"]
    monkeypatch.setitem(OFFLINE_HANDLERS, "wealth.suitability",
                        lambda t: {**real(t), "payload": {**real(t)["payload"], "suggested_route": "proceed"}})
    ev2 = evaluate(wf, app)
    assert ev2["gate"] == "fail" and {"suitability_routing", "kyc_block"} <= set(ev2["critical_failures"])


def test_factory_delivers_the_wealth_advice_api(wf):
    spec = load_spec()
    run_spec(wf, spec, tenant="wealth", po="po.wendy")
    d = run_delivery(wf, spec, tenant="wealth")
    r = run_release(wf, spec, d["pr_id"], tenant="wealth")
    assert r["state"] == "SUCCEEDED", r["error"]
    rel = wf.release.get(r["release_id"])
    assert rel["manifest"]["spec"]["id"] == "SPEC-WEALTH-ADVICE-API" and rel["project"] == "wealth-advisory"
