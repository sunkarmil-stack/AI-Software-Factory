"""Retail account risk product on the factory, end to end (offline, synthetic data).

1. Domain pack: onboard `retail-account-risk` (tenant retail, risk tier 3), propose its ontology; owners approve.
2. The factory delivers the Risk Cases API (spec -> agents -> CI -> nonprod) from the service template.
3. Monitoring: score four accounts for fraud, AML indicators and credit early warning, with reason codes.
4. Fraud: investigate, open a case, analyst confirms a 48-hour restriction on outgoing payments.
5. AML: structuring and the 24-hour cash threshold route the case to the AML officer.
6. Credit: early-warning case, limit reduction approved by a credit officer with 30 days' notice.
7. Pipeline C gate for the product agents.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .core.util import REPO_ROOT
from .demo import run_delivery, run_release, run_spec, say
from .domains import load_domain
from .factory import Factory
from .product.retailrisk import RetailRiskApp, evaluate

APPROVERS = (("onto.olivia", "ontology_owner"), ("arch.alex", "architect"), ("po.priya", "product_owner"),
             ("data.dana", "data_owner"), ("sec.sam", "security"))
ACCOUNTS = ["acc-100", "acc-101", "acc-102", "acc-103"]


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
    out = load_domain(f, "retail-account-risk")
    approve_pack(f)
    return out


def main(home: str | None = None) -> Factory:
    f = Factory(Path(home) if home else None)
    print("== Retail account risk product on the AI Software Factory ==")
    out = setup(f)
    say(f"domain pack: project {out['project']} (tenant retail, risk tier 3); {len(out['proposals'])} ontology "
        f"proposals approved by owners; {len(f.ontology.proposals('pending'))} pending")

    spec = yaml.safe_load((REPO_ROOT / "samples" / "specs" / "risk-cases-api.v1.yaml").read_text(encoding="utf-8"))
    run_spec(f, spec, tenant="retail", po="po.priya")
    d = run_delivery(f, spec, tenant="retail")
    r = run_release(f, spec, d["pr_id"], tenant="retail")
    say(f"delivery: {spec['spec_id']} built from templates/fastapi-service, CI-verified, released {r['release_id']} -> {r['state']}")

    app = RetailRiskApp(f)
    alerts = app.monitor(ACCOUNTS)
    say(f"monitoring {len(ACCOUNTS)} accounts -> {len(alerts)} alerts:")
    for a in alerts:
        say(f"  {a['account_id']} {a['type']:<6} score {a['score']:>3}  {[x['code'] for x in a['reasons']]}")
    by = {(a["account_id"], a["type"]): a for a in alerts}

    fraud = by[("acc-101", "fraud")]
    inv = app.investigate("risk.fay", fraud["alert_id"])
    say(f"fraud acc-101: investigator -> {inv['suggested_next_step']} | {inv['summary'][:110]}...")
    case = app.open_case("risk.fay", fraud["alert_id"])
    say(f"  OpenRiskCase: {case['status']} -> {case['result']['case_id']}")
    t = app.restriction_ticket("risk.fay", case["request_id"], "outgoing_payments", 48)
    conf = f.approvals.approve("risk.fay", "fraud_analyst", t["binding"], rationale="customer unreachable; losses ongoing")
    rs = app.restrict("risk.fay", t, [conf])
    say(f"  RestrictAccount (analyst-confirmed, 48h): {rs['status']} -> {rs['result']['restriction_id']}")

    weak = by[("acc-102", "fraud")]
    c2 = app.open_case("risk.fay", weak["alert_id"])
    t2 = app.restriction_ticket("risk.fay", c2["request_id"])
    rs2 = app.restrict("risk.fay", t2, [f.approvals.approve("risk.fay", "fraud_analyst", t2["binding"])])
    say(f"fraud acc-102 (score 45, vulnerable customer): restriction {rs2['status']} — {rs2['messages'][0]}")

    aml = by[("acc-103", "aml")]
    inv3 = app.investigate("risk.fay", aml["alert_id"])
    c3 = app.open_case("risk.fay", aml["alert_id"])
    say(f"AML acc-103: investigator -> {inv3['suggested_next_step']}; OpenRiskCase {c3['status']} needs {c3.get('roles')}")
    f.actions.approve(c3["request_id"], "aml.ann", "aml_officer")
    say(f"  AML officer takes the case: {app.open_case('risk.fay', aml['alert_id'], request_id=c3['request_id'])['status']}")

    credit = by[("acc-102", "credit")]
    inv4 = app.investigate("cred.cal", credit["alert_id"])
    c4 = app.open_case("cred.cal", credit["alert_id"])
    say(f"credit acc-102: EWS {credit['score']}, IFRS 9 stage indicator {credit['ews']['ifrs9_stage_indicator']} -> "
        f"{inv4['suggested_next_step']}; case {c4['status']}")
    lt = app.limit_ticket("cred.cal", c4["request_id"], new_limit_cents=2300000, notice_days=30)
    ok = f.approvals.approve("cred.ola", "credit_officer", lt["binding"], rationale="EWS 80; stage 2 indicator")
    red = app.reduce_limit("cred.cal", lt, [ok])
    say(f"  ReduceCreditLimit $25,000 -> $23,000 with 30 days' notice (credit officer): {red['status']}")

    ev = evaluate(f, app)
    say(f"Pipeline C (product agents): gate {ev['gate']} scores {ev['scores']}")
    say(f"evidence chain valid: {f.ledger.verify_chain()[0]}")
    return f
