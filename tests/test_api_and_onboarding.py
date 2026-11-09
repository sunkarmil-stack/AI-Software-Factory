"""Control-plane API (NFR 02 durable run id, authz, auditor read-only) and onboarding (REQ 056, ACC 10 automated part)."""
import json
import socket
import threading
import time
import urllib.request

import pytest
import uvicorn

from factory.api import create_app
from factory.demo import OWNERS, load_spec


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture
def api(onboarded):
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(onboarded), host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True); t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield onboarded, f"http://127.0.0.1:{port}"
    server.should_exit = True; t.join(5)


def call(base, method, path, token=None, body=None):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json", **({"authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def test_api_spec_submission_to_baseline(api):
    f, base = api
    assert call(base, "GET", "/healthz")[0] == 200
    assert call(base, "POST", "/v1/runs", None, {})[0] == 401
    spec = load_spec(1)
    st, body = call(base, "POST", "/v1/runs", "dev-eng.erin", {"workflow": "spec_pipeline", "project": "accounts", "input": {"spec": spec}})
    assert st == 403  # only product owners submit specifications
    t0 = time.time()
    st, body = call(base, "POST", "/v1/runs", "dev-po.priya", {"workflow": "spec_pipeline", "project": "accounts", "input": {"spec": spec}})
    assert st == 202 and body["run_id"].startswith("RUN-") and time.time() - t0 < 1.0
    rid = body["run_id"]
    for _ in range(100):
        st, run = call(base, "GET", f"/v1/runs/{rid}", "dev-po.priya")
        if run["state"] == "WAITING_APPROVAL":
            break
        time.sleep(0.05)
    assert run["state"] == "WAITING_APPROVAL"
    st, err = call(base, "POST", f"/v1/runs/{rid}/approvals", "dev-eng.erin", {"step_id": "approve_baseline", "role": "product_owner"})
    assert st == 403
    assert call(base, "POST", f"/v1/runs/{rid}/approvals", "dev-po.priya", {"step_id": "approve_baseline", "role": "product_owner"})[0] == 201
    assert call(base, "POST", f"/v1/runs/{rid}/approvals", "dev-arch.alex", {"step_id": "approve_baseline", "role": "architect"})[0] == 201
    for _ in range(100):
        st, run = call(base, "GET", f"/v1/runs/{rid}", "dev-po.priya")
        if run["state"] == "SUCCEEDED":
            break
        time.sleep(0.05)
    assert run["state"] == "SUCCEEDED"
    # auditors read evidence but cannot act
    assert call(base, "GET", "/v1/audit/export?project=accounts", "dev-aud.ada")[0] == 200
    assert call(base, "POST", "/v1/ops/controls", "dev-aud.ada", {"kind": "revoke_tool", "target": "kb"})[0] == 403
    assert call(base, "POST", f"/v1/runs/{rid}/cancel", "dev-aud.ada")[0] == 403
    st, snap = call(base, "GET", "/v1/metrics", "dev-ops.omar")
    assert set(snap) >= {"factory", "delivery", "product"}
    with urllib.request.urlopen(base + "/dashboard") as r:
        assert b"Product agent health" in r.read()


def test_onboarding_requires_accountable_owners(f):
    with pytest.raises(ValueError, match="owners missing"):
        f.onboard("pilot-2", "retail", {"product_owner": "po.priya"}, 1, 10.0)
    with pytest.raises(ValueError, match="does not hold"):
        f.onboard("pilot-2", "retail", {**OWNERS, "release_owner": "eng.eli"}, 1, 10.0)
    with pytest.raises(PermissionError):
        f.onboard("pilot-2", "retail", OWNERS, 1, 10.0, actor="eng.erin")
    out = f.onboard("pilot-2", "retail", {**OWNERS, "engineer": "eng.eli"}, 1, 10.0)
    p = f.project("pilot-2")
    assert out["repo_head"] and p["owners"]["engineer"] == "eng.eli" and p["skills"] and p["budget_usd"] == 10.0


def test_second_team_golden_path(f):
    """ACC 10 (automated portion): a second pilot team runs the golden path with its own owners."""
    from factory.demo import run_spec, run_delivery, run_release
    owners = {**OWNERS, "engineer": "eng.eli", "release_owner": "rel.ravi"}
    f.onboard("cards", "retail", owners, risk_tier=1, budget_usd=20.0)
    spec = load_spec(1); spec["project"] = "cards"; spec["spec_id"] = "SPEC-CARDS-API"; spec["risk_tier"] = 1
    run_spec(f, spec)
    d = f.engine.start("delivery", "cards", "retail", "eng.eli", {"spec_id": spec["spec_id"], "version": 1})
    assert f.engine.run(d) == "SUCCEEDED"
