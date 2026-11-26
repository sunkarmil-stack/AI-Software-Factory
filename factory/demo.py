"""End-to-end thin vertical slice (Section 12):
approved request -> specified FastAPI service change -> agents implement in isolation ->
trusted CI validates -> human authorizes the bound artifact -> service deploys to nonprod.
"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

from .core.util import REPO_ROOT
from .factory import Factory

OWNERS = {"product_owner": "po.priya", "architect": "arch.alex", "engineer": "eng.erin", "qa_owner": "qa.quinn",
          "release_owner": "rel.rita", "operator": "ops.omar"}


def load_spec(version: int = 1) -> dict:
    return yaml.safe_load((REPO_ROOT / "samples" / "specs" / f"accounts-api.v{version}.yaml").read_text(encoding="utf-8"))


def spec_v2(v1: dict) -> dict:
    """ACC 01: change an authorization criterion after implementation (step-up auth on reads)."""
    s = copy.deepcopy(v1)
    s["version"] = 2
    s["contracts"][0]["endpoints"][0]["require_mfa"] = True
    for r in s["requirements"]:
        if r["id"] == "REQ-2":
            r["statement"] = ("The service SHALL deny account reads with 403 when the caller lacks accounts:read "
                              "or has not completed step-up authentication.")
            r["acceptance_criteria"].append({
                "id": "AC-7", "given": "a caller with accounts:read but without step-up authentication",
                "when": "it requests account acc-100", "then": "the request is denied with 403",
                "check": {"request": {"method": "GET", "path": "/accounts/acc-100",
                                      "headers": {"X-Principal": "chan-1", "X-Tenant": "retail", "X-Scopes": "accounts:read"}},
                          "expect": {"status": 403}}})
        if r["id"] in ("REQ-1", "REQ-3"):
            for ac in r["acceptance_criteria"]:
                if ac["check"]["request"]["path"].startswith("/accounts/acc-"):
                    ac["check"]["request"]["headers"]["X-MFA"] = "true"
    s["history"].append({"version": 2, "change": "REQ-2 requires step-up authentication for reads"})
    return s


def say(msg: str) -> None:
    print(f"  {msg}")


def run_spec(f: Factory, spec: dict, tenant: str = "retail", po: str = "po.priya", arch: str = "arch.alex",
             qa: str = "qa.quinn") -> str:
    rid = f.engine.start("spec_pipeline", spec["project"], tenant, spec["owner"], {"spec": spec, "spec_id": spec["spec_id"],
                                                                                    "version": spec["version"]})
    st = f.engine.run(rid)
    if st != "WAITING_APPROVAL":
        raise RuntimeError(f"spec pipeline: {st} {f.engine.get(rid)['error']}")
    binding = f.engine.get(rid)["steps"]["approve_baseline"]["output"]["request"]["binding"]
    ids = [f.approvals.approve(po, "product_owner", binding, rationale="scope and ACs accepted"),
           f.approvals.approve(arch, "architect", binding, rationale="ADR-1 and threat model reviewed")]
    f.engine.provide_approvals(rid, "approve_baseline", ids)
    st = f.engine.run(rid)
    if st != "SUCCEEDED":
        raise RuntimeError(f"spec pipeline: {st} {f.engine.get(rid)['error']}")
    f.ci.qa.materialize(spec, qa)
    return rid


def run_delivery(f: Factory, spec: dict, tenant: str = "retail", **extra) -> dict:
    rid = f.engine.start("delivery", spec["project"], tenant, "eng.erin",
                         {"spec_id": spec["spec_id"], "version": spec["version"], **extra})
    st = f.engine.run(rid)
    run = f.engine.get(rid)
    if st != "SUCCEEDED":
        raise RuntimeError(f"delivery: {st} {run['error']}")
    return {"run": rid, **run["steps"]["push_pr"]["output"], "findings": run["steps"]["review"]["output"]["findings"]}


def run_release(f: Factory, spec: dict, pr_id: str, env: str = "nonprod", approver: str = "rel.rita",
                tenant: str = "retail") -> dict:
    rid = f.engine.start("release", spec["project"], tenant, "eng.erin",
                         {"spec_id": spec["spec_id"], "version": spec["version"], "pr_id": pr_id, "env": env})
    assert f.engine.run(rid) == "WAITING_APPROVAL"
    b = f.engine.get(rid)["steps"]["approve_review"]["output"]["request"]["binding"]
    f.engine.provide_approvals(rid, "approve_review", [f.approvals.approve("eng.erin", "engineer", b,
                                                                            rationale="diff and evidence reviewed")])
    st = f.engine.run(rid)
    if st != "WAITING_APPROVAL":
        raise RuntimeError(f"release: {st} {f.engine.get(rid)['error']}")
    run = f.engine.get(rid)
    b = run["steps"]["approve_release"]["output"]["request"]["binding"]
    f.engine.provide_approvals(rid, "approve_release", [f.approvals.approve(approver, "release_owner", b,
                                                                             rationale="evidence complete")])
    st = f.engine.run(rid)
    run = f.engine.get(rid)
    return {"run": rid, "state": st, "error": run["error"], "release_id": run["steps"]["package"]["output"]["release_id"],
            "advice": run["steps"]["package"]["output"]["advice"],
            "deployment": run["steps"]["promote"]["output"]}


def main(home: str | None = None) -> Factory:
    f = Factory(Path(home) if home else None)
    print("== AI Software Factory: thin vertical slice ==")
    b = f.bootstrap()
    say(f"catalog: {len(f.skills.list())} skills published (signed), agent bundles {b['agents'] or 'already registered'}")
    if not f.db.one("SELECT id FROM projects WHERE id='accounts'"):
        f.onboard("accounts", "retail", OWNERS, risk_tier=2, budget_usd=50.0)
    say("project 'accounts' onboarded (tenant retail, risk tier 2, template fastapi-service)")
    spec = load_spec(1)
    run_spec(f, spec)
    say(f"Pipeline A: {spec['spec_id']}@v1 validated, analysed, threat-modelled, planned, baselined by PO+architect")
    say("QA owner published protected acceptance suite")
    d = run_delivery(f, spec)
    say(f"Stage 3: agents implemented in sandbox -> {d['pr_id']} @ {d['head_sha'][:10]} "
        f"({len(d['findings'])} advisory review findings)")
    r = run_release(f, spec, d["pr_id"])
    say(f"Stages 4-6: engineer review -> trusted CI -> merge -> CI -> {r['release_id']} -> release owner approval")
    say(f"release advisor: {r['advice']}")
    say(f"deployment: {r['state']} {r['deployment']}")
    from .ops import audit
    rec = audit.reconstruct(f, r["release_id"])
    say(f"audit: {len(rec['chain'])} linked trace nodes; evidence chain valid={rec['evidence_chain_valid']}")
    print("== done. Start the control-plane API with: python -m factory.cli serve ==")
    return f


if __name__ == "__main__":
    main()


def production_demo(home: str | None = None) -> Factory:
    """Production profile end to end against the local Azure stand-in (Entra ID, Key Vault, AI gateway, OTLP).
    Database and sandbox use owned, expiring exemptions because PostgreSQL and AKS are not available locally;
    the stand-in serves plain HTTP, so the ai_gateway TLS check is exempted too."""
    import tempfile
    from pathlib import Path
    from . import config
    from .core.tracing import TRACER
    from .core.util import now
    from .enterprise.standin import AzureStandIn
    home = Path(home or tempfile.mkdtemp(prefix="asf-prod-"))
    s = AzureStandIn().start()
    try:
        cfg = s.production_settings(home, sandbox_backend="process")
        cfg["exemptions"] = [{"check": c, "owner": "platform-lead", "reason": "local demo, no Azure",
                              "expires": now() + 3600} for c in ("database", "sandbox", "ai_gateway")]
        f = Factory(home / "factory", settings=config.load(None, **cfg))
        f.bootstrap()
        say("preflight: " + ", ".join(f"{c.name}={'PASS' if c.ok else 'EXEMPT'}" for c in f.preflight))
        f.onboard("accounts", "retail", OWNERS, risk_tier=2, budget_usd=50.0)
        spec = load_spec(1)
        run_spec(f, spec)
        d = run_delivery(f, spec)
        r = run_release(f, spec, d["pr_id"])
        rid = r["release_id"]
        say(f"nonprod release {rid}: {r['state']} via {r['deployment']['target']}")
        b = f.release.binding(rid, "prod", "CHG0012345")
        apps = [f.approvals.approve("rel.ravi", "release_owner", b), f.approvals.approve("chg.carla", "change_manager", b)]
        out = f.release.promote(rid, "prod", apps, "eng.erin", change_ref="CHG0012345")
        say(f"prod promotion under CHG0012345: {out['status']}; GitOps desired digest "
            f"{f.release.target.desired('accounts', 'prod')['digest'][:19]}...")
        calls = [c for c in s.requests if c["path"].endswith("/chat/completions")]
        say(f"enterprise AI gateway: {len(calls)} model calls, all as workload "
            f"{sorted({c['caller'] for c in calls})}, all with traceparent={all(c['traceparent'] for c in calls)}")
        say(f"Key Vault sign operations: {sum(1 for c in s.requests if c['path'].endswith('/sign'))}; "
            f"OTLP spans exported: {TRACER.flush() + len(s.spans)}")
        say(f"evidence chain valid: {f.ledger.verify_chain()[0]}")
        return f
    finally:
        s.stop()
