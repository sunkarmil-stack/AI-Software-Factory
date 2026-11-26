"""Production profile: enterprise AI gateway, Entra ID, Key Vault, GitOps promotion, queue workers,
OTLP telemetry, fail-closed preflight. Runs over real HTTP against the local Azure stand-in.

Exemptions used here (each owned, justified, expiring - the same mechanism production uses):
  database   : no PostgreSQL server in the build environment (translator/DDL tested separately)
  sandbox    : no Kubernetes cluster; manifest is tested, execution uses the process backend
  ai_gateway : the stand-in serves plain HTTP on loopback
"""
import json
import threading
import time
import urllib.request

import pytest
import uvicorn

from factory import config
from factory.config import PreflightError
from factory.core.db import pg_ddl, translate
from factory.core.tracing import TRACER
from factory.core.util import REPO_ROOT, now
from factory.delivery.release import PromotionDenied
from factory.demo import OWNERS, load_spec, run_delivery, run_release, run_spec
from factory.enterprise.standin import FACTORY_AUD, AzureStandIn
from factory.factory import Factory
from factory.models.gateway import ContentRejected, ModelContext, ProviderUnavailable

EXEMPT = ["database", "sandbox", "ai_gateway"]


@pytest.fixture(scope="module")
def standin():
    s = AzureStandIn().start()
    yield s
    s.stop()


def prod_settings(standin, home, **extra):
    cfg = standin.production_settings(home, sandbox_backend="process", **extra)
    cfg["exemptions"] = [{"check": c, "owner": "platform-lead", "reason": "reference build environment",
                          "expires": now() + 3600} for c in EXEMPT]
    return config.load(None, **cfg)


@pytest.fixture
def pf(standin, tmp_path):
    f = Factory(tmp_path / "home", settings=prod_settings(standin, tmp_path))
    f.bootstrap()
    return f


# ---------------------------------------------------------------- fail-closed start
def test_production_refuses_to_start_without_controls(tmp_path, standin):
    with pytest.raises(PreflightError) as e:
        Factory(tmp_path / "h1", settings=config.Settings(profile="production"))
    names = {c.name for c in e.value.failures}
    assert {"database", "auth", "signer", "ai_gateway", "model_egress", "sandbox", "deployment_target",
            "telemetry", "execution"} <= names
    expired = prod_settings(standin, tmp_path)
    expired.exemptions[0]["expires"] = now() - 1
    with pytest.raises(PreflightError, match="database"):
        Factory(tmp_path / "h2", settings=expired)


def test_production_has_no_direct_model_egress(pf):
    assert set(pf.models.providers) == {"ai_gateway"}
    assert all(d["provider"] == "ai_gateway" for d in pf.models.deployments.values())
    assert all(p.token is None for p in pf.directory.principals.values())        # dev tokens disabled
    assert pf.ledger.query(kind="factory.preflight")[0]["data"]["checks"]


# ---------------------------------------------------------------- end-to-end production slice
def test_production_slice_through_enterprise_services(pf, standin):
    n0 = len(standin.requests)
    pf.onboard("accounts", "retail", OWNERS, risk_tier=2, budget_usd=50.0)
    spec = load_spec(1)
    run_spec(pf, spec)
    d = run_delivery(pf, spec)
    r = run_release(pf, spec, d["pr_id"])
    assert r["state"] == "SUCCEEDED" and r["deployment"]["target"] == "gitops"
    calls = standin.requests[n0:]
    model_calls = [c for c in calls if c["path"].endswith("/chat/completions")]
    assert model_calls and all(c["caller"] == "factory-workload" for c in model_calls)
    assert all(c["traceparent"] and c["run"] for c in model_calls)                 # correlation reaches the gateway
    signs = [c for c in calls if c["path"].endswith("/sign")]
    assert {c["key"] for c in signs} >= {"factory-ci-builder", "factory-release", "factory-approval"}
    rel = pf.release.get(r["release_id"])
    assert rel["signature"].startswith("kv:")
    from factory.ops.audit import reconstruct
    rec = reconstruct(pf, r["release_id"])
    assert rec["provenance_verified"] and rec["evidence_chain_valid"]
    desired = pf.release.target.desired("accounts", "nonprod")
    assert desired["digest"] == rel["manifest"]["artifact"]["digest"] and desired["canaryWeight"] == 100
    assert TRACER.flush() >= 0 and any(s["name"].startswith("step.") for s in standin.spans)

    # ---- production promotion: change record + release owner + change manager, nonprod first
    rid = r["release_id"]
    ro = pf.approvals.approve("rel.ravi", "release_owner", pf.release.binding(rid, "prod", "CHG0012345"))
    with pytest.raises(PromotionDenied) as e:
        pf.release.promote(rid, "prod", [ro], "eng.erin")                          # no change record
    assert "change record" in str(e.value)
    with pytest.raises(PromotionDenied, match="change_manager"):
        pf.release.promote(rid, "prod", [ro], "eng.erin", change_ref="CHG0012345")
    cm = pf.approvals.approve("chg.carla", "change_manager", pf.release.binding(rid, "prod", "CHG0012345"))
    with pytest.raises(PromotionDenied, match="binding mismatch"):                  # approvals bound to the change record
        pf.release.promote(rid, "prod", [ro, cm], "eng.erin", change_ref="CHG0099999")
    out = pf.release.promote(rid, "prod", [ro, cm], "eng.erin", change_ref="CHG0012345")
    assert out["status"] == "active" and pf.release.target.desired("accounts", "prod")["stage"] == "full"
    assert pf.release.drift("accounts", "prod") == []


def test_prod_requires_nonprod_first(delivered):
    f, spec, d, r = delivered
    # build a second release that never reached nonprod
    rel2 = f.release.create("accounts", f.release.get(r["release_id"])["manifest"]["evidence"]["build"],
                            spec["spec_id"], d["pr_id"], "eng.erin")
    f.db.execute("UPDATE deployments SET status='rolled_back'")
    with pytest.raises(PromotionDenied, match="nonprod first"):
        f.release.promote(rel2["id"], "prod", [], "eng.erin", change_ref="CHG0012345")


# ---------------------------------------------------------------- enterprise controls seen by the factory
def _mctx(f, purpose="delivery.planning"):
    return ModelContext(f.directory.get("agent:planning"), "p", "retail", "r", "r", purpose, "internal")


def test_enterprise_revocation_falls_back_then_fails_closed(pf, standin):
    task = {"kind": "planning.plan", "spec": load_spec(1)}
    standin.revoked.add("model:claude-delivery")
    try:
        c = pf.models.complete(_mctx(pf), "delivery-default", "sys", task)
        assert c.fallback_used and c.deployment == "delivery-fallback"
        assert pf.ledger.query(kind="model.enterprise_revoked")
        standin.revoked.add("model:gpt-delivery")
        with pytest.raises(ProviderUnavailable, match="AssetDisabled|disabled"):
            pf.models.complete(_mctx(pf), "delivery-default", "sys", task)
    finally:
        standin.revoked -= {"model:claude-delivery", "model:gpt-delivery"}


def test_enterprise_content_safety_and_quota(pf, standin):
    with pytest.raises(ContentRejected, match="content safety"):
        pf.models.complete(_mctx(pf), "delivery-default", "sys", {"kind": "planning.plan", "note": "BLOCK-ME-CONTENT"})
    standin.token_limits["factory-workload"] = 1
    try:
        with pytest.raises(ProviderUnavailable, match="throttled"):
            pf.models.complete(_mctx(pf), "delivery-default", "sys", {"kind": "planning.plan", "spec": load_spec(1)})
        assert any(json.loads(r["labels"]).get("category") == "provider_throttled"
                   for r in pf.db.all("SELECT labels FROM metrics WHERE name='provider_error'"))
    finally:
        standin.token_limits.clear(); standin.usage.clear()


def test_ontology_action_reaches_system_of_record_via_gateway(pf, standin):
    from factory.ontology.discovery import discover
    pf.onboard("accounts", "retail", OWNERS, risk_tier=2, budget_usd=50.0)
    discover(pf, load_spec(1), [{"id": "glossary.md", "text": (REPO_ROOT / "samples/ontology/glossary.md").read_text()}])
    for p in pf.ontology.proposals("pending"):
        for u, role in (("onto.olivia", "ontology_owner"), ("arch.alex", "architect"), ("po.priya", "product_owner")):
            q = pf.ontology.proposal(p["id"])
            if q["status"] == "pending" and role in q["roles"]:
                pf.ontology.approve(p["id"], u, role)
    before = len(standin.holds)
    r = pf.actions.execute("CreateHold", "ops.omar", {"id": "acc-100", "tenant": "retail"},
                           {"amount_cents": 500, "reason": "fraud review"})
    assert r["status"] == "executed" and r["result"]["hold_id"].startswith("HLD-SOR-")
    again = pf.actions.execute("CreateHold", "ops.omar", {"id": "acc-100", "tenant": "retail"},
                               {"amount_cents": 500, "reason": "fraud review"}, request_id=r["request_id"])
    assert again["result"]["hold_id"] == r["result"]["hold_id"] and len(standin.holds) == before + 1
    hit = [x for x in standin.requests if x.get("api") == "deposits"][-1]
    assert hit["idempotency_key"].startswith("action:") and hit["caller"] == "factory-workload"
    standin.revoked.add("api:deposits")                                           # enterprise kill switch
    try:
        from factory.tools.gateway import PermanentError
        with pytest.raises(PermanentError, match="revoked by the enterprise AI gateway"):
            pf.actions.execute("CreateHold", "ops.omar", {"id": "acc-101", "tenant": "retail"},
                               {"amount_cents": 700, "reason": "aml review"})
        assert pf.ledger.query(kind="tool.enterprise_revoked")
    finally:
        standin.revoked.discard("api:deposits")


def test_key_vault_signing_is_scoped(pf, standin):
    from factory.core.crypto import SigningDenied
    with pytest.raises(SigningDenied):
        pf.keys.sign("agent:implementation", b"x")
    sig = pf.keys.sign("ci-builder", b"payload")
    assert pf.keys.verify("ci-builder", b"payload", sig) and not pf.keys.verify("ci-builder", b"tampered", sig)
    assert not pf.keys.verify("release-service", b"payload", sig)                 # other key
    standin.kv_sign_acl["factory-ci-builder"] = set()                             # Key Vault RBAC removed
    try:
        with pytest.raises(PermissionError, match="403"):
            pf.keys.sign("ci-builder", b"payload")
    finally:
        standin.kv_sign_acl["factory-ci-builder"] = {"factory-workload"}
    assert pf.secret_store.get("prod-db-password") == "kv-held-secret-value"


# ---------------------------------------------------------------- API: Entra auth, readiness, queue workers
def _call(base, method, path, token=None, body=None):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json", **({"authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"null"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null"), dict(e.headers)


def test_api_entra_auth_readiness_and_queue_workers(pf, standin):
    from factory.api import create_app
    from tests.test_api_and_onboarding import _free_port
    pf.onboard("accounts", "retail", OWNERS, risk_tier=2, budget_usd=50.0)
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(create_app(pf), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True); t.start()
    while not srv.started:
        time.sleep(0.02)
    base = f"http://127.0.0.1:{port}"
    try:
        st, body, hdr = _call(base, "GET", "/readyz")
        assert st == 200 and set(body["checks"]["exemptions"]) == set(EXEMPT)
        assert hdr.get("x-content-type-options") == "nosniff" and "max-age" in hdr.get("strict-transport-security", "")
        assert _call(base, "GET", "/v1/metrics", "dev-ops.omar")[0] == 401           # dev tokens rejected
        bad_aud = standin.issue_user_token("ada", ["Factory.Auditor"], ["grp-retail"], audience="api://other")
        assert _call(base, "GET", "/v1/metrics", bad_aud)[0] == 401
        expired = standin.issue_user_token("ada", ["Factory.Auditor"], ["grp-retail"], ttl=-120)
        assert _call(base, "GET", "/v1/metrics", expired)[0] == 401
        aud = standin.issue_user_token("ada", ["Factory.Auditor"], ["grp-retail"])
        assert _call(base, "GET", "/v1/evidence/verify", aud)[1]["valid"] is True
        po = standin.issue_user_token("po.priya", ["Factory.ProductOwner"], ["grp-retail"])
        st, body, _ = _call(base, "POST", "/v1/runs", po, {"workflow": "spec_pipeline", "project": "accounts",
                                                            "input": {"spec": load_spec(1)}})
        assert st == 202
        rid = body["run_id"]
        assert pf.engine.get(rid)["state"] == "PENDING"                            # API does not execute work
        worker = Factory(pf.home, settings=pf.settings, worker_id="worker-a")         # separate worker process
        assert worker.engine.work(max_runs=1) == 1
        assert pf.engine.get(rid)["state"] == "WAITING_APPROVAL"
        arch = standin.issue_user_token("arch.alex", ["Factory.Architect"], ["grp-retail"])
        assert _call(base, "POST", f"/v1/runs/{rid}/approvals", po, {"step_id": "approve_baseline", "role": "product_owner"})[0] == 201
        assert _call(base, "POST", f"/v1/runs/{rid}/approvals", arch, {"step_id": "approve_baseline", "role": "architect"})[0] == 201
        assert pf.engine.get(rid)["state"] == "PENDING"
        worker.engine.work(max_runs=1)
        assert pf.engine.get(rid)["state"] == "SUCCEEDED"
    finally:
        srv.should_exit = True; t.join(5)


# ---------------------------------------------------------------- components tested without live services
def test_postgres_translation_and_migrations():
    assert translate("INSERT OR IGNORE INTO trace_edges(src,dst,rel) VALUES(?,?,?)") == \
        "INSERT INTO trace_edges(src,dst,rel) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING"
    up = translate("INSERT OR REPLACE INTO specs(spec_id,version,digest,body) VALUES(?,?,?,?)")
    assert up.endswith("ON CONFLICT (spec_id, version) DO UPDATE SET digest=EXCLUDED.digest, body=EXCLUDED.body")
    committed = (REPO_ROOT / "migrations/postgres/001_init.sql").read_text()
    assert committed == pg_ddl(), "schema drift: run `python -m factory.cli migrate --emit`"
    assert "AUTOINCREMENT" not in committed and "GENERATED ALWAYS AS IDENTITY" in committed
    assert "BEFORE UPDATE OR DELETE ON evidence" in (REPO_ROOT / "migrations/postgres/002_evidence_append_only.sql").read_text()


def test_kubernetes_sandbox_manifest(tmp_path):
    from factory.core.db import DB
    from factory.core.evidence import Ledger, ObjectStore
    from factory.delivery.sandbox import SandboxService
    led = Ledger(DB(tmp_path / "x.db"), ObjectStore(tmp_path / "o"))
    sb = SandboxService(tmp_path / "s", led, backend="kubernetes", runtime_class="kata-vm-isolation").create("accounts", "t", 2)
    m = sb.job_manifest(["python", "-m", "pytest"])
    spec = m["spec"]["template"]["spec"]
    assert spec["runtimeClassName"] == "kata-vm-isolation" and spec["automountServiceAccountToken"] is False
    c = spec["containers"][0]
    assert c["securityContext"]["readOnlyRootFilesystem"] and c["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert m["spec"]["activeDeadlineSeconds"] == 300 and spec["securityContext"]["runAsNonRoot"]


def test_harness_helios_adapter_is_explicitly_unconfigured(f):
    from factory.delivery.targets import NotConfigured, make_target
    t = make_target(f.release, "harness-helios")
    with pytest.raises(NotConfigured, match="Harness/Helios"):
        t.apply("accounts", "prod", {})


def test_policy_requires_change_manager_for_prod(f):
    d = f.policy.evaluate({"actor": {"kind": "workload", "id": "release-service"}, "action": "deploy.execute",
                           "side_effect": "write", "env": "prod", "reversible": True})
    assert d.allow and d.obligations["approval_roles"] == ["release_owner", "change_manager"]
    n = f.policy.evaluate({"actor": {"kind": "workload", "id": "release-service"}, "action": "deploy.execute",
                           "side_effect": "write", "env": "nonprod", "reversible": True})
    assert n.obligations["approval_roles"] == ["release_owner"]


def test_postgres_uses_entra_token_not_password(standin, tmp_path):
    """psycopg is absent here; verify the password provider the factory wires into PostgresDB."""
    import jwt
    from factory.enterprise.auth import PG_AAD_AUDIENCE, WorkloadTokenProvider
    w = standin.production_settings(tmp_path)["workload"]
    tp = WorkloadTokenProvider(w["client_id"], w["tenant_id"], w["token_file"], w["authority_host"])
    claims = jwt.decode(tp.token(PG_AAD_AUDIENCE), options={"verify_signature": False})
    assert claims["aud"] == PG_AAD_AUDIENCE and claims["azp"] == "factory-workload"
    src = (REPO_ROOT / "factory/factory.py").read_text()
    assert "token(PG_AAD_AUDIENCE)" in src and "open_db(st.database_url, self.home / \"factory.db\", pg_pw)" in src
