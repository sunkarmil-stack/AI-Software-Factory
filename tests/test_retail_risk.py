"""Retail account risk product on the factory: domain pack and governed ontology, explainable fraud scoring, AML
indicators routed to the AML officer, credit early warning with an IFRS 9 stage indicator, analyst-confirmed
restrictions, credit-officer limit reductions, agent output checks, isolation, evaluation and the generated API."""
import pytest

from factory.demo_retailrisk import ACCOUNTS, approve_pack
from factory.domains import load_domain
from factory.models.gateway import OFFLINE_HANDLERS
from factory.ontology.actions import ActionDenied
from factory.product.retailrisk import RetailRiskApp, aml_indicators, credit_ews, evaluate, fraud_features, scoring
from factory.tools.gateway import CallContext, ToolDenied


@pytest.fixture
def kf(f):
    load_domain(f, "retail-account-risk")
    approve_pack(f)
    return f


@pytest.fixture
def app(kf):
    return RetailRiskApp(kf)


@pytest.fixture
def alerts(app):
    return {(a["account_id"], a["type"]): a for a in app.monitor(ACCOUNTS)}


def confirmed_restriction(kf, app, case_req, **kw):
    t = app.restriction_ticket("risk.fay", case_req, **kw)
    return t, [kf.approvals.approve("risk.fay", "fraud_analyst", t["binding"])]


# ---------------------------------------------------------------- domain pack and governance
def test_domain_pack_is_governed(f):
    out = load_domain(f, "retail-account-risk")
    assert not out["invalid"] and len(out["proposals"]) == 21 and "RestrictAccount" not in f.ontology.all_actions()
    consequential = [p for p in out["proposals"] if p["kind"] == "add_action"
                     and p["payload"]["action"]["side_effect"] == "consequential"]
    assert len(consequential) == 2 and all("security" in p["roles"] for p in consequential)
    approve_pack(f)
    assert not f.ontology.proposals("pending")


# ---------------------------------------------------------------- deterministic, explainable scoring
def test_fraud_score_is_explainable_and_exact(alerts):
    a = alerts[("acc-101", "fraud")]
    assert a["score"] == 100 and sum(r["points"] for r in a["reasons"]) == 105        # capped at 100
    assert {r["code"] for r in a["reasons"]} == set(scoring()["fraud"]["weights"])
    assert alerts[("acc-102", "fraud")]["score"] == 45
    assert ("acc-100", "fraud") not in alerts and ("acc-103", "fraud") not in alerts


def test_aml_indicators_follow_the_24_hour_window():
    cfg = scoring()
    deposits = [{"ts": f"2026-11-25T{h:02d}:00:00Z", "amount_cents": 950000, "channel": "cash_deposit"} for h in (1, 9, 20)]
    ind = aml_indicators({"transactions": deposits}, cfg)
    assert ind["lctr_threshold_met"] and ind["suspected_structuring"] and ind["cash_24h_cents"] == 2850000
    spread = [dict(d, ts=f"2026-11-{20 + 2 * i}T09:00:00Z") for i, d in enumerate(deposits)]      # 48h apart
    ind2 = aml_indicators({"transactions": spread}, cfg)
    assert not ind2["lctr_threshold_met"] and not ind2["suspected_structuring"]


def test_credit_early_warning_and_ifrs9_stage_indicator(alerts):
    assert alerts[("acc-101", "credit")]["ews"]["ifrs9_stage_indicator"] == 3            # 95 days past due
    a = alerts[("acc-102", "credit")]
    assert a["score"] == 80 and a["ews"]["ifrs9_stage_indicator"] == 2                    # 35 days past due
    healthy = {"facility": "credit_card", "limit_cents": 100, "balance_cents": 10, "days_past_due": 0,
               "bureau_score": 700, "bureau_score_at_origination": 760, "missed_payments_6m": 0, "nsf_6m": 0}
    assert credit_ews(healthy, scoring())["ifrs9_stage_indicator"] == 2                   # score-drop SICR trigger


# ---------------------------------------------------------------- fraud cases and restrictions
def test_restriction_needs_case_evidence_and_bound_confirmation(app, kf, alerts):
    case = app.open_case("risk.fay", alerts[("acc-101", "fraud")]["alert_id"])
    assert case["status"] == "executed"
    t = app.restriction_ticket("risk.fay", case["request_id"])
    with pytest.raises(ActionDenied):
        app.restrict("risk.fay", t, [])                                                   # tier 3: no confirmation
    conf = kf.approvals.approve("risk.fay", "fraud_analyst", t["binding"])
    out = app.restrict("risk.fay", t, [conf])
    assert out["status"] == "executed" and out["result"]["status"] == "active"
    with pytest.raises(ActionDenied, match="already has an active restriction"):
        app.restriction_ticket("risk.fay", case["request_id"])


def test_low_score_cannot_justify_a_restriction(app, alerts):
    case = app.open_case("risk.fay", alerts[("acc-102", "fraud")]["alert_id"])
    t = app.restriction_ticket("risk.fay", case["request_id"])
    r = app.restrict("risk.fay", t, [])
    assert r["status"] == "blocked" and "below the restriction threshold" in r["messages"][0]


def test_long_restriction_goes_to_fraud_manager(app, kf, alerts):
    case = app.open_case("risk.fay", alerts[("acc-101", "fraud")]["alert_id"])
    t, conf = confirmed_restriction(kf, app, case["request_id"], duration_hours=120)
    r = app.restrict("risk.fay", t, conf)
    assert r["status"] == "pending_approval" and r["roles"] == ["fraud_manager"]
    with pytest.raises(Exception):
        kf.actions.approve(r["request_id"], "risk.fay", "fraud_manager")                 # analyst is not a manager
    assert kf.actions.approve(r["request_id"], "risk.max", "fraud_manager")["status"] == "approved"
    assert app.restrict("risk.fay", t, conf, request_id=r["request_id"])["status"] == "executed"


# ---------------------------------------------------------------- AML
def test_aml_indicators_route_to_the_aml_officer(app, kf, alerts):
    a = alerts[("acc-103", "aml")]
    assert {r["code"] for r in a["reasons"]} == {"CASH_24H_THRESHOLD", "STRUCTURING_PATTERN"}
    r = app.open_case("risk.fay", a["alert_id"])
    assert r["status"] == "pending_approval" and r["roles"] == ["aml_officer"]
    kf.actions.approve(r["request_id"], "aml.ann", "aml_officer")
    assert app.open_case("risk.fay", a["alert_id"], request_id=r["request_id"])["status"] == "executed"
    with pytest.raises(ActionDenied, match="not an executed fraud case"):
        app.restriction_ticket("risk.fay", r["request_id"])                              # AML cases cannot restrict


# ---------------------------------------------------------------- credit limit reductions
def test_limit_reduction_rules_and_credit_officer(app, kf, alerts):
    case = app.open_case("cred.cal", alerts[("acc-102", "credit")]["alert_id"])
    below = app.limit_ticket("cred.cal", case["request_id"], new_limit_cents=2000000, notice_days=30)
    assert app.reduce_limit("cred.cal", below, [])["status"] == "blocked"              # below the balance
    short = app.limit_ticket("cred.cal", case["request_id"], new_limit_cents=2300000, notice_days=10)
    assert "notice period" in app.reduce_limit("cred.cal", short, [])["messages"][0]
    ok = app.limit_ticket("cred.cal", case["request_id"], new_limit_cents=2300000, notice_days=30)
    with pytest.raises(ActionDenied, match="credit_officer"):
        app.reduce_limit("cred.cal", ok, [])
    with pytest.raises(Exception):
        kf.approvals.approve("cred.cal", "credit_officer", ok["binding"])                 # maker cannot check
    appr = kf.approvals.approve("cred.ola", "credit_officer", ok["binding"])
    out = app.reduce_limit("cred.cal", ok, [appr])
    assert out["status"] == "executed" and out["result"]["effective_date"] == "+30d"


def test_roles_are_enforced(app, alerts):
    with pytest.raises(PermissionError):
        app.open_case("cred.cal", alerts[("acc-101", "fraud")]["alert_id"])              # credit analyst, fraud alert
    with pytest.raises(PermissionError):
        app.investigate("adv.ada", alerts[("acc-101", "fraud")]["alert_id"])


# ---------------------------------------------------------------- agents and isolation
def test_investigation_rejects_invented_numbers_and_accusations(app, alerts, monkeypatch):
    a = alerts[("acc-101", "fraud")]
    assert app.investigate("risk.fay", a["alert_id"])["status"] == "ready_for_analyst"
    real = OFFLINE_HANDLERS["risk.investigate"]

    def bad(task):
        out = real(task)
        out["payload"]["summary"] += " The customer is a fraudster; expected losses $4,500."
        return out
    monkeypatch.setitem(OFFLINE_HANDLERS, "risk.investigate", bad)
    inv = app.investigate("risk.fay", a["alert_id"])
    assert inv["status"] == "needs_review" and inv["summary"] == ""
    assert any("fraudster" in i for i in inv["issues"]) and any("4,500" in i for i in inv["issues"])


def test_agents_and_other_tenants_cannot_act_or_read(kf):
    agent = kf.directory.get("agent:fraud-investigator")
    ctx = CallContext(agent, "retail", "retail-account-risk", "r", "nonprod", purpose="product")
    with pytest.raises(ToolDenied):
        kf.tools.invoke(ctx, "transactions", "recent", {"account_id": "acc-101"})
    with pytest.raises(ActionDenied, match="no grant"):
        kf.actions.execute("OpenRiskCase", "agent:fraud-investigator", {"id": "acc-101", "tenant": "retail"}, {},
                           tenant="retail")
    wctx = CallContext(kf.directory.get("agent:advisor-copilot"), "wealth", "wealth-advisory", "r", "nonprod", purpose="product")
    with pytest.raises(ToolDenied):
        kf.tools.invoke(wctx, "transactions", "recent", {"account_id": "acc-101"})


def test_product_evaluation_gate(app, kf, monkeypatch):
    assert evaluate(kf, app)["gate"] == "pass"
    real = OFFLINE_HANDLERS["risk.investigate"]
    monkeypatch.setitem(OFFLINE_HANDLERS, "risk.investigate",
                        lambda t: {**real(t), "payload": {**real(t)["payload"], "suggested_next_step": "close_no_action"}})
    ev = evaluate(kf, app)
    assert ev["gate"] == "fail" and ev["scores"]["next_step"] == 0.0


def test_scoring_matches_the_reviewed_configuration():
    cfg = scoring()
    data = {"profile": {"avg_txn_cents_90d": 100, "home_country": "CA", "known_devices": ["d"], "known_payees": []},
            "transactions": [{"ts": "2026-11-26T09:00:00Z", "amount_cents": 100, "channel": "card_present",
                              "country": "CA", "device": "d", "payee": "x", "mcc": "5411"}]}
    assert fraud_features(data, cfg) == (0, [])


# ---------------------------------------------------------------- generated API
def test_factory_delivers_the_risk_cases_api(kf):
    import yaml
    from factory.core.util import REPO_ROOT
    from factory.delivery.repo import git
    from factory.demo import run_delivery, run_release, run_spec
    spec = yaml.safe_load((REPO_ROOT / "samples" / "specs" / "risk-cases-api.v1.yaml").read_text(encoding="utf-8"))
    run_spec(kf, spec, tenant="retail", po="po.priya")
    d = run_delivery(kf, spec, tenant="retail")
    r = run_release(kf, spec, d["pr_id"], tenant="retail")
    assert r["state"] == "SUCCEEDED", r.get("error")
    routes = git(["show", "main:app/routes.py"], kf.repo.bare("retail-account-risk"))
    assert 'STORE.list(c.tenant, "cases")' in routes and "customer_id" in routes
    assert 'raise HTTPError(409, "status must be open")' in routes
