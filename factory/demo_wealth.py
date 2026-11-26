"""Wealth-advisory product built on the factory, end to end (offline, synthetic data).

1. Domain pack: onboard project `wealth-advisory` (tenant wealth, risk tier 3) and propose its ontology.
2. Owners approve concepts, suitability rules and actions (nothing applies before approval).
3. Factory delivers the Wealth Advice API through Pipelines A, B and D to nonprod.
4. Advisor workflow: review prep -> suitability -> recommendation (rules + named approvers) -> order.
5. Pipeline C gate for the product agents.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .core.util import REPO_ROOT
from .demo import run_delivery, run_release, run_spec, say
from .domains import load_domain
from .factory import Factory
from .product.wealth import WealthAdvisoryApp, evaluate

APPROVERS = (("onto.olivia", "ontology_owner"), ("arch.alex", "architect"), ("po.wendy", "product_owner"),
             ("data.dana", "data_owner"), ("sec.sam", "security"))


def load_spec() -> dict:
    return yaml.safe_load((REPO_ROOT / "samples" / "specs" / "wealth-advice-api.v1.yaml").read_text())


def approve_pack(f) -> int:
    """Each owner approves the proposals that name their role (separation of duties is enforced by the registry)."""
    n = 0
    for _ in range(3):   # rules/actions wait for their concepts, then apply
        for p in f.ontology.proposals("pending"):
            for user, role in APPROVERS:
                q = f.ontology.proposal(p["id"])
                if q["status"] == "pending" and role in q["roles"] and user != q["proposer"] and \
                        not any(a.get("role") == role for a in q.get("approvals", []) if isinstance(a, dict)):
                    try:
                        f.ontology.approve(p["id"], user, role); n += 1
                    except Exception:
                        pass
    return n


def setup(f) -> dict:
    f.bootstrap()
    out = load_domain(f, "wealth-advisory")
    approve_pack(f)
    return out


def main(home: str | None = None) -> Factory:
    f = Factory(Path(home) if home else None)
    print("== Wealth advisory product on the AI Software Factory ==")
    out = setup(f)
    pend = f.ontology.proposals("pending")
    say(f"domain pack: project {out['project']} onboarded (tenant wealth, risk tier 3); "
        f"{len(out['proposals'])} ontology proposals, {len(pend)} still pending after owner approvals")
    say(f"ontology: rules {sorted(r for r in f.ontology.all_rules() if 'RECOMMENDATION' in r or 'TRADEORDER' in r)}")

    spec = load_spec()
    run_spec(f, spec, tenant="wealth", po="po.wendy")
    d = run_delivery(f, spec, tenant="wealth")
    r = run_release(f, spec, d["pr_id"], tenant="wealth")
    say(f"delivery: {spec['spec_id']} baselined, built by agents, CI-verified, released {r['release_id']} -> {r['state']}")

    app = WealthAdvisoryApp(f)
    rv = app.prepare_review("adv.ada", "ACC-W-1001")
    say(f"review ACC-W-1001: {rv['status']} | {rv['review_notes']}")
    for p in rv["proposals"]:
        say(f"  draft: {p['side']} {p['product_code']} ${p['amount_cents'] / 100:,.2f} — {p['basis']}")

    buy = next(p for p in rv["proposals"] if p["side"] == "buy")
    s = app.suitability("adv.ada", "ACC-W-1001", buy)
    say(f"suitability ({buy['product_code']}): {s['suggested_route']} — {s['findings'][0]}")
    rec = app.recommend("adv.ada", "ACC-W-1001", buy, s["rationale_draft"] + " Restores fixed income toward the IPS target.")
    say(f"ProposeRecommendation: {rec['status']} -> {rec.get('result', {}).get('recommendation_id')}")
    ticket = app.order_ticket("adv.ada", rec["request_id"])
    conf = f.approvals.approve("adv.ada", "advisor", ticket["binding"], rationale="client instruction confirmed by phone")
    order = app.place_order("adv.ada", rec["request_id"], [conf])
    say(f"PlaceTradeOrder: {order['status']} -> {order.get('result', {}).get('order_id')}")

    tech = {"product_code": "TECH-THEME", "side": "buy", "amount_cents": 500000}
    t = app.recommend("adv.ada", "ACC-W-1001", tech, "Client requested thematic technology exposure; documented in meeting notes.")
    say(f"TECH-THEME (risk 5 > tolerance 3): {t['status']} needs {t.get('roles')}")
    f.actions.approve(t["request_id"], "sup.sid", "supervisor")
    t2 = app.recommend("adv.ada", "ACC-W-1001", tech, "", request_id=t["request_id"])
    say(f"  after branch supervisor approval: {t2['status']}")

    c = app.recommend("adv.ada", "ACC-W-1001", {"product_code": "CRYPTO-ETF", "side": "buy", "amount_cents": 100000},
                      "Client asked for digital asset exposure after reading about it in the news.")
    say(f"CRYPTO-ETF (no know-your-product review): {c['status']} — {c['messages'][0]}")
    k = app.recommend("adv.ben", "ACC-W-1002", {"product_code": "CDN-BOND-AGG", "side": "buy", "amount_cents": 100000},
                      "Adds to fixed income in line with the client's medium time horizon.")
    say(f"ACC-W-1002 (KYC 430 days old): {k['status']} — {k['messages'][0]}")

    ev = evaluate(f, app)
    say(f"Pipeline C (product agents): gate {ev['gate']} scores {ev['scores']}")
    say(f"evidence chain valid: {f.ledger.verify_chain()[0]}")
    return f
