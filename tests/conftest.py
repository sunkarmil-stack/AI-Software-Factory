import pytest

from factory.demo import OWNERS, load_spec, run_delivery, run_release, run_spec
from factory.factory import Factory


@pytest.fixture
def f(tmp_path):
    fac = Factory(tmp_path / "home")
    fac.bootstrap()
    return fac


@pytest.fixture
def onboarded(f):
    f.onboard("accounts", "retail", OWNERS, risk_tier=2, budget_usd=50.0)
    return f


@pytest.fixture
def baselined(onboarded):
    spec = load_spec(1)
    run_spec(onboarded, spec)
    return onboarded, spec


@pytest.fixture
def delivered(baselined):
    f, spec = baselined
    d = run_delivery(f, spec)
    r = run_release(f, spec, d["pr_id"])
    assert r["state"] == "SUCCEEDED", r
    return f, spec, d, r


@pytest.fixture(autouse=True)
def _reset_clock():
    import factory.core.util as u
    u._CLOCK_OFFSET = 0.0
    yield
    u._CLOCK_OFFSET = 0.0
