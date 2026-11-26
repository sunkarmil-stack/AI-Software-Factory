"""OSFI regulatory reporting product on the factory: domain pack and governed ontology, Databricks lakehouse via
an approved MCP server with pinned tools, data quality, deterministic calculation, edit checks, lineage, variance
commentary, certification rules, maker-checker filing, amendments, reproducibility and the evaluation gate."""
import pytest

from factory.demo_regreport import approve_pack, file_with_signoff
from factory.domains import load_domain
from factory.models.gateway import OFFLINE_HANDLERS
from factory.ontology.actions import ActionDenied
from factory.product.regreport import (RegReportingApp, calculate, evaluate, template, ungrounded)
from factory.tools.gateway import CallContext, ToolDenied

TODAY = "2026-11-12"
DRIVER = "Higher client derivative volumes in rates trading during the quarter."


@pytest.fixture
def rf(f):
    load_domain(f, "osfi-regulatory-reporting")
    approve_pack(f)
    return f


@pytest.fixture
def app(rf):
    return RegReportingApp(rf, today=TODAY)


def explained(app, run, driver=DRIVER):
    for v in run["variances"]:
        app.explain("fin.pia", run["run_id"], v["cell_id"], driver)
    return run


def fix_lcr_source(f):
    f.databricks.publish_version("regrep.gold.hqla", "2026-10-31", {"level2b": 12000})
    f.databricks.publish_version("regrep.gold.load_log", "2026-10-31", {"hqla": ["complete", "2026-11-05"]})


# ---------------------------------------------------------------- domain pack and governance
def test_domain_pack_is_governed(f):
    out = load_domain(f, "osfi-regulatory-reporting")
    assert f.project("osfi-regulatory-reporting")["tenant"] == "finance" and not out["invalid"]
    assert len(out["proposals"]) == 16 and "CertifyReturn" not in f.ontology.all_actions()
    filing = next(p for p in out["proposals"] if p["kind"] == "add_action" and p["payload"]["action"]["name"] == "FileReturn")
    assert "security" in filing["roles"]
    approve_pack(f)
    assert not f.ontology.proposals("pending") and {"CertifyReturn", "FileReturn"} <= set(f.ontology.all_actions())


# ---------------------------------------------------------------- Databricks via MCP
def test_lakehouse_reads_go_through_the_approved_mcp_server(app, rf):
    run = app.prepare("fin.pia", "LR", "2026-10-31")
    ts = rf.mcp_toolsets["databricks-regrep"]
    assert "regrep__gold__run_sql" in ts.untrusted_metadata["tools"]                    # listed by the server...
    assert {c["tool"] for c in rf.databricks.calls} <= set(rf.mcp_servers["databricks-regrep"]["pinned_tools"])  # ...never called
    assert set(rf.tools.contracts["lakehouse"]["actions"]) and \
        all(a["side_effect"] == "read" for a in rf.tools.contracts["lakehouse"]["actions"].values())  # no write path
    assert run["versions"] == {"capital_components": 41, "gl_balances": 203, "leverage_exposures": 58, "load_log": 90}


def test_agents_and_other_tenants_cannot_read_the_lakehouse(rf):
    for pid, tenant, project in (("agent:reg-variance-analyst", "finance", "osfi-regulatory-reporting"),
                                 ("agent:rag-assistant", "retail", "accounts")):
        ctx = CallContext(rf.directory.get(pid), tenant, project, "r", "nonprod", purpose="product")
        with pytest.raises(ToolDenied):
            rf.tools.invoke(ctx, "lakehouse", "capital_components", {"period_end": "2026-10-31"})
    with pytest.raises(PermissionError):
        RegReportingApp(rf, today=TODAY).prepare("adv.ada", "LR", "2026-10-31")


def test_changed_mcp_tool_definition_is_refused(rf):
    from factory.enterprise.databricks_standin import DatabricksUCStandIn
    rf.databricks = DatabricksUCStandIn()
    rf.databricks.schema_override["regrep__gold__hqla"] = {"type": "object", "properties": {"sql": {"type": "string"}}}
    rf.mcp_toolsets["databricks-regrep"].transport = None                                # force a fresh connection
    with pytest.raises(Exception, match="changed since approval|re-approval"):
        RegReportingApp(rf, today=TODAY).prepare("fin.pia", "LA", "2026-10-31")
    assert rf.ledger.query(kind="mcp.tool_changed")


def test_server_identity_is_checked(rf):
    rf.databricks.fingerprint = "sha256:imposter"
    rf.mcp_toolsets["databricks-regrep"].transport = None
    with pytest.raises(Exception, match="identity mismatch"):
        RegReportingApp(rf, today=TODAY).prepare("fin.pia", "LR", "2026-10-31")


# ---------------------------------------------------------------- calculation, lineage, edit checks
def test_calculations_lineage_and_edit_checks(app):
    run = app.prepare("fin.pia", "LR", "2026-10-31")
    cells = {c["key"]: c["value"] for c in run["cells"]}
    assert cells["tier1_capital"] == 62300 and cells["total_exposure"] == 1455700 and run["headline"] == 4.28
    assert run["floor"] == 3.5                                                   # 3% minimum + 0.5% D-SIB buffer
    assert all(c["passed"] for c in run["edit_checks"]) and run["lineage_complete"]
    src = run["lineage"]["LR.070"]
    assert {s["table"] for s in src} == {"regrep.gold.leverage_exposures"} and all(s["version"] == 58 for s in src)
    assert [v["cell_id"] for v in run["variances"]] == ["LR.030"]                 # derivatives +17.3%, +9,000


def test_lcr_caps_apply(rf):
    tpl = template("LA")
    base = {"level1": 100000, "level2a": 120000, "level2b": 80000, "retail_deposits_balance": 100000,
            "retail_deposits_runoff": 0.1, "wholesale_unsecured_balance": 100000, "wholesale_unsecured_runoff": 0.4,
            "secured_funding_balance": 0, "secured_funding_runoff": 0, "committed_facilities_balance": 0,
            "committed_facilities_runoff": 0, "inflows": 90000}
    _, env = calculate(tpl, base)
    assert env["level2_capped"] == round(2 / 3 * 100000, 2)                       # Level 2 capped at 40% of HQLA
    assert env["inflows_capped"] == 37500                                         # inflows capped at 75% of outflows


def test_cross_return_check_fails_when_capital_disagrees(app, rf):
    rf.databricks.publish_version("regrep.gold.capital_components", "2026-10-31", {"tier1_reported_car": 62000})
    run = explained(app, app.prepare("fin.pia", "LR", "2026-10-31"))
    assert not next(c for c in run["edit_checks"] if c["id"] == "EC-LR-01")["passed"]
    r = app.certify("fin.pia", run["run_id"])
    assert r["status"] == "blocked" and any("edit check" in m for m in r["messages"])


# ---------------------------------------------------------------- data quality
def test_critical_dq_blocks_and_triage_routes_to_owner(app, rf):
    run = app.prepare("fin.pia", "LA", "2026-10-31")
    assert run["status"] == "dq_blocked" and {b["check"] for b in run["dq"]} == {"DQ-COMPLETENESS", "DQ-LOAD-STATUS"}
    assert {t["owner"] for t in run["triage"]} == {"treasury-data-team"}
    r = app.certify("fin.pia", run["run_id"])
    assert r["status"] == "blocked" and "critical data-quality" in r["messages"][0]
    fix_lcr_source(rf)
    run2 = app.prepare("fin.pia", "LA", "2026-10-31")
    assert run2["status"] == "awaiting_explanations" and run2["snapshot_id"] != run["snapshot_id"]
    assert run2["headline"] == 133.333


def test_reconciliation_warning_needs_data_owner(app, rf):
    rf.databricks.publish_version("regrep.gold.gl_balances", "2026-10-31", {"total_assets_in_scope": 1218000})  # 0.25%
    run = explained(app, app.prepare("fin.pia", "LR", "2026-10-31"))
    assert [b["severity"] for b in run["dq"]] == ["warning"]
    r = app.certify("fin.pia", run["run_id"])
    assert r["status"] == "pending_approval" and r["roles"] == ["data_owner"]
    rf.actions.approve(r["request_id"], "data.dana", "data_owner")
    assert app.certify("fin.pia", run["run_id"], request_id=r["request_id"])["status"] == "executed"


# ---------------------------------------------------------------- certification rules
def test_unexplained_variance_blocks_and_only_preparer_explains(app):
    run = app.prepare("fin.pia", "LR", "2026-10-31")
    r = app.certify("fin.pia", run["run_id"])
    assert r["status"] == "blocked" and any("variances" in m for m in r["messages"])
    with pytest.raises(ValueError):
        app.explain("fin.pia", run["run_id"], "LR.030", "volumes")
    with pytest.raises(PermissionError):
        app.explain("fin.raj", run["run_id"], "LR.030", DRIVER)


def test_ratio_below_requirement_is_escalated_not_suppressed(app, rf):
    fix_lcr_source(rf)
    rf.databricks.publish_version("regrep.gold.hqla", "2026-10-31", {"level1": 120000})
    run = explained(app, app.prepare("fin.pia", "LA", "2026-10-31"), "Large deposit outflow drew down Level 1 assets.")
    assert run["headline"] < 100
    r = app.certify("fin.pia", run["run_id"])
    assert r["status"] == "pending_approval" and r["roles"] == ["senior_officer"]
    rf.actions.approve(r["request_id"], "cfo.cam", "senior_officer")
    ok = app.certify("fin.pia", run["run_id"], request_id=r["request_id"])
    assert ok["status"] == "executed"
    assert file_with_signoff(rf, app, ok["request_id"])["status"] == "executed"   # a breach is still filed


def test_late_filing_needs_senior_officer(rf):
    fix_lcr_source(rf)
    late = RegReportingApp(rf, today="2026-11-20")                                 # LA deadline is 2026-11-14
    run = explained(late, late.prepare("fin.pia", "LA", "2026-10-31"), "Term wholesale funding rolled into the window.")
    r = late.certify("fin.pia", run["run_id"])
    assert r["status"] == "pending_approval" and r["roles"] == ["senior_officer"]


# ---------------------------------------------------------------- filing controls
def test_filing_needs_reviewer_and_senior_officer_bound_to_package(app, rf):
    run = explained(app, app.prepare("fin.pia", "LR", "2026-10-31"))
    cert = app.certify("fin.pia", run["run_id"])
    with pytest.raises(ActionDenied):
        app.file("fin.pia", cert["request_id"], [])
    t = app.filing_ticket("fin.pia", cert["request_id"])
    rev = rf.approvals.approve("fin.raj", "reg_reviewer", t["binding"])
    with pytest.raises(ActionDenied, match="senior_officer"):
        app.file("fin.pia", cert["request_id"], [rev])
    with pytest.raises(Exception):
        rf.approvals.approve("fin.pia", "senior_officer", t["binding"])            # preparer holds no such role
    so = rf.approvals.approve("cfo.cam", "senior_officer", t["binding"])
    out = app.file("fin.pia", cert["request_id"], [rev, so])
    assert out["status"] == "executed" and out["result"]["receipt"].startswith("RRS-")
    with pytest.raises(ActionDenied, match="already been filed"):
        app.filing_ticket("fin.pia", cert["request_id"])


def test_amendment_requires_a_reason(app, rf):
    run = explained(app, app.prepare("fin.pia", "LR", "2026-10-31"))
    file_with_signoff(rf, app, app.certify("fin.pia", run["run_id"])["request_id"])
    rf.databricks.publish_version("regrep.gold.leverage_exposures", "2026-10-31", {"off_balance_sheet_after_ccf": 88500})
    run2 = explained(app, app.prepare("fin.pia", "LR", "2026-10-31"))
    r = app.certify("fin.pia", run2["run_id"])
    assert r["status"] == "blocked" and any("amendment" in m for m in r["messages"])
    ok = app.certify("fin.pia", run2["run_id"], amendment_reason="Late-booked letters of credit added to off-balance sheet exposures.")
    assert ok["status"] == "executed"


# ---------------------------------------------------------------- reproducibility, commentary, evaluation
def test_filed_return_reproduces_after_source_changes(app, rf):
    run = app.prepare("fin.pia", "LR", "2026-10-31")
    rf.databricks.publish_version("regrep.gold.leverage_exposures", "2026-10-31", {"derivatives": 70000})
    assert app.reproduce(run["run_id"])["identical"]                               # time travel to pinned versions
    assert app.prepare("fin.pia", "LR", "2026-10-31")["snapshot_id"] != run["snapshot_id"]


def test_commentary_cannot_invent_numbers_or_drivers(app, monkeypatch):
    real = OFFLINE_HANDLERS["regrep.variance"]

    def bad(task):
        out = real(task)
        out["payload"]["commentary"][0]["draft"] += " This was due to hedging, adding 12,500."
        return out
    monkeypatch.setitem(OFFLINE_HANDLERS, "regrep.variance", bad)
    run = app.prepare("fin.pia", "LR", "2026-10-31")
    assert run["variances"][0]["draft"] == "" and run["issues"]
    assert ungrounded("increased by 12,500", [run["variances"][0]]) == [12500.0]


def test_product_evaluation_gate(app, rf, monkeypatch):
    assert evaluate(rf, app)["gate"] == "pass"
    real = OFFLINE_HANDLERS["regrep.variance"]

    def driver(task):
        out = real(task)
        for c in out["payload"]["commentary"]:
            c["draft"] += " Driven by market volatility."
        return out
    monkeypatch.setitem(OFFLINE_HANDLERS, "regrep.variance", driver)
    ev = evaluate(rf, app)
    assert ev["gate"] == "fail" and "no_invented_drivers" in ev["critical_failures"]


def test_production_routes_mcp_through_the_enterprise_ai_gateway(tmp_path):
    from factory import config
    from factory.core.util import now
    from factory.enterprise.ai_gateway import MCPHttpTransport
    from factory.enterprise.standin import AzureStandIn
    from factory.factory import Factory
    s = AzureStandIn().start()
    try:
        cfg = s.production_settings(tmp_path, sandbox_backend="process")
        cfg["exemptions"] = [{"check": c, "owner": "platform-lead", "reason": "test", "expires": now() + 3600}
                             for c in ("database", "sandbox", "ai_gateway")]
        pf = Factory(tmp_path / "h", settings=config.load(None, **cfg))
        assert isinstance(pf.mcp_toolsets["databricks-regrep"].factory(), MCPHttpTransport)
        assert next(c for c in pf.preflight if c.name == "tool_egress").ok
    finally:
        s.stop()


def test_factory_delivers_the_regulatory_returns_api(rf):
    import yaml
    from factory.core.util import REPO_ROOT
    from factory.delivery.repo import git
    from factory.demo import run_delivery, run_release, run_spec
    spec = yaml.safe_load((REPO_ROOT / "samples" / "specs" / "regulatory-returns-api.v1.yaml").read_text(encoding="utf-8"))
    run_spec(rf, spec, tenant="finance", po="po.fran")
    d = run_delivery(rf, spec, tenant="finance")
    r = run_release(rf, spec, d["pr_id"], tenant="finance")
    assert r["state"] == "SUCCEEDED", r.get("error")
    routes = git(["show", "main:app/routes.py"], rf.repo.bare("osfi-regulatory-reporting"))
    assert 'STORE.get(c.tenant, req.params["return_id"], "returns")' in routes
    assert "reviewer_notes" in routes and 'raise HTTPError(409, "status must be certified")' in routes
    assert rf.release.get(r["release_id"])["manifest"]["spec"]["id"] == "SPEC-REGULATORY-RETURNS-API"
