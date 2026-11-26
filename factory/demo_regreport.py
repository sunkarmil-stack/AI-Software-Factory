"""OSFI regulatory reporting product on the factory, end to end (offline, synthetic lakehouse).

1. Domain pack: onboard `osfi-regulatory-reporting` (tenant finance, risk tier 3), propose its ontology.
2. Owners approve concepts, reporting rules and actions.
3. The factory delivers the Regulatory Returns API (spec -> agents -> CI -> nonprod) from the service template.
4. Leverage return (LR), Q4 2026: lakehouse via MCP -> DQ -> calculate -> edit checks -> variance draft
   -> preparer explains -> certify -> reviewer + senior officer approve -> file.
4. LCR return (LA), October 2026: critical DQ break blocks certification; triage routes it to the data owner;
   the fix lands as a new Delta version; re-run, certify, file.
5. Audit: reproduce the filed LR return from pinned versions. 6. Pipeline C gate for the product agents.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .core.util import REPO_ROOT
from .demo import run_delivery, run_release, run_spec, say
from .domains import load_domain
from .factory import Factory
from .product.regreport import RegReportingApp, evaluate

APPROVERS = (("onto.olivia", "ontology_owner"), ("arch.alex", "architect"), ("po.fran", "product_owner"),
             ("data.dana", "data_owner"), ("sec.sam", "security"))
TODAY = "2026-11-12"


def approve_pack(f) -> None:
    for _ in range(3):
        for p in f.ontology.proposals("pending"):
            for user, role in APPROVERS:
                q = f.ontology.proposal(p["id"])
                if q["status"] == "pending" and role in q["roles"] and user != q["proposer"]:
                    try:
                        f.ontology.approve(p["id"], user, role)
                    except Exception:
                        pass


def setup(f) -> dict:
    f.bootstrap()
    out = load_domain(f, "osfi-regulatory-reporting")
    approve_pack(f)
    return out


def file_with_signoff(f, app, cert_request_id: str) -> dict:
    t = app.filing_ticket("fin.pia", cert_request_id)
    approvals = [f.approvals.approve("fin.raj", "reg_reviewer", t["binding"], rationale="reviewed package and lineage"),
                 f.approvals.approve("cfo.cam", "senior_officer", t["binding"], rationale="attested for filing")]
    return app.file("fin.pia", cert_request_id, approvals)


def main(home: str | None = None) -> Factory:
    f = Factory(Path(home) if home else None)
    print("== OSFI regulatory reporting product on the AI Software Factory ==")
    out = setup(f)
    say(f"domain pack: project {out['project']} (tenant finance, risk tier 3); {len(out['proposals'])} ontology "
        f"proposals approved by owners; {len(f.ontology.proposals('pending'))} pending")
    spec = yaml.safe_load((REPO_ROOT / "samples" / "specs" / "regulatory-returns-api.v1.yaml").read_text(encoding="utf-8"))
    run_spec(f, spec, tenant="finance", po="po.fran")
    d = run_delivery(f, spec, tenant="finance")
    r = run_release(f, spec, d["pr_id"], tenant="finance")
    say(f"delivery: {spec['spec_id']} baselined, built from templates/fastapi-service, CI-verified, "
        f"released {r['release_id']} -> {r['state']}")
    app = RegReportingApp(f, today=TODAY)

    lr = app.prepare("fin.pia", "LR", "2026-10-31")
    say(f"MCP: Databricks server {f.mcp_toolsets['databricks-regrep'].name} connected; unapproved tools ignored: "
        f"{sorted(set(f.mcp_toolsets['databricks-regrep'].untrusted_metadata['tools']) - set(f.mcp_servers['databricks-regrep']['pinned_tools']))}")
    say(f"LR 2026-10-31: snapshot {lr['snapshot_id']} pinned {lr['versions']}")
    say(f"  leverage ratio {lr['headline']}% vs requirement {lr['floor']}%; DQ breaks {len(lr['dq'])}; "
        f"edit checks {sum(c['passed'] for c in lr['edit_checks'])}/{len(lr['edit_checks'])} pass; status {lr['status']}")
    for v in lr["variances"]:
        say(f"  material variance {v['cell_id']}: {v['draft']}")
        app.explain("fin.pia", lr["run_id"], v["cell_id"], "Higher client derivative volumes in rates trading during Q4.")
    c = app.certify("fin.pia", lr["run_id"])
    say(f"  CertifyReturn: {c['status']} -> {c.get('result', {}).get('certification_id')}")
    fil = file_with_signoff(f, app, c["request_id"])
    say(f"  FileReturn (reviewer + senior officer bound to package): {fil['status']} receipt {fil['result']['receipt']}")

    la = app.prepare("fin.pia", "LA", "2026-10-31")
    say(f"LA 2026-10-31: status {la['status']}; breaks {[(b['check'], b['table'].split('.')[-1]) for b in la['dq']]}")
    for t in la["triage"]:
        say(f"  triage: {t['check']} -> {t['owner']}: {t['next_step']}")
    blocked = app.certify("fin.pia", la["run_id"])
    say(f"  CertifyReturn attempt: {blocked['status']} — {blocked['messages'][0]}")
    v1 = f.databricks.publish_version("regrep.gold.hqla", "2026-10-31", {"level2b": 12000})
    v2 = f.databricks.publish_version("regrep.gold.load_log", "2026-10-31", {"hqla": ["complete", "2026-11-05"]})
    say(f"  data owner fixes at source: hqla v{v1}, load_log v{v2}")
    la2 = app.prepare("fin.pia", "LA", "2026-10-31")
    say(f"  re-run: snapshot {la2['snapshot_id']}; LCR {la2['headline']}% vs {la2['floor']}%; status {la2['status']}")
    for v in la2["variances"]:
        say(f"  material variance {v['cell_id']}: {v['draft']}")
        app.explain("fin.pia", la2["run_id"], v["cell_id"], "Term wholesale funding rolled into the 30-day window.")
    c2 = app.certify("fin.pia", la2["run_id"])
    fil2 = file_with_signoff(f, app, c2["request_id"])
    say(f"  certified {c2['status']}, filed {fil2['status']} receipt {fil2['result']['receipt']}")

    rep = app.reproduce(lr["run_id"])
    say(f"audit: LR re-computed from pinned versions -> identical={rep['identical']} ({rep['snapshot_id']})")
    ev = evaluate(f, app)
    say(f"Pipeline C (product agents): gate {ev['gate']} scores {ev['scores']}")
    say(f"evidence chain valid: {f.ledger.verify_chain()[0]}")
    return f
