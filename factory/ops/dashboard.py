"""Operational dashboard data + HTML (REQ 054, REQ 058).

Three separate planes so operators don't treat every failure as an LLM issue:
factory health, delivery outcomes, product agent health; failures are categorized.
"""
from __future__ import annotations

import html
import json

from ..core.db import unj
from ..core.util import now


def snapshot(f) -> dict:
    db = f.db
    runs = {r["state"]: r["n"] for r in db.all("SELECT state, COUNT(*) n FROM runs GROUP BY state")}
    cats: dict[str, dict[str, float]] = {}
    for r in db.all("SELECT plane, labels, SUM(value) v FROM metrics GROUP BY plane, labels"):
        c = unj(r["labels"]).get("category")
        if c:
            cats.setdefault(r["plane"], {}).setdefault(c, 0)
            cats[r["plane"]][c] += r["v"]
    ok, n = f.ledger.verify_chain()
    builds = {r["status"]: r["n"] for r in db.all("SELECT status, COUNT(*) n FROM builds GROUP BY status")}
    deps = {r["status"]: r["n"] for r in db.all("SELECT status, COUNT(*) n FROM deployments GROUP BY status")}
    lead = db.all("SELECT s.created s0, d.created d0 FROM deployments d JOIN releases r ON r.id=d.release_id "
                  "JOIN specs s ON s.project=d.project WHERE d.status IN ('active','superseded')")
    cycle = sorted(x["d0"] - x["s0"] for x in lead)
    rework = db.one("SELECT COUNT(*) n FROM runs WHERE workflow='delivery'")["n"]
    prs = db.one("SELECT COUNT(*) n FROM prs WHERE status='merged'")["n"]
    usage = db.one("SELECT COALESCE(SUM(cost),0) c, COALESCE(SUM(units),0) u FROM usage")
    reviews = {r["status"]: r["n"] for r in db.all("SELECT status, COUNT(*) n FROM review_items WHERE kind='rag_review' GROUP BY status")}
    denied = db.one("SELECT COUNT(*) n FROM evidence WHERE kind='tool.denied'")["n"]
    return {
        "generated": now(),
        "factory": {"runs_by_state": runs, "dead_letter_queue": db.one("SELECT COUNT(*) n FROM dlq WHERE replayed=0")["n"],
                    "active_controls": db.one("SELECT COUNT(*) n FROM controls WHERE active=1")["n"],
                    "evidence_chain_valid": ok, "evidence_records": n, "failure_categories": cats.get("factory", {})},
        "delivery": {"builds": builds, "merged_prs": prs, "deployments": deps,
                     "median_cycle_time_s": cycle[len(cycle) // 2] if cycle else None,
                     "delivery_runs_per_merged_pr": round(rework / prs, 2) if prs else None,
                     "open_review_findings": db.one("SELECT COUNT(*) n FROM review_items WHERE kind='review_finding' AND status='open'")["n"],
                     "model_cost_usd": round(usage["c"], 4), "model_tokens": usage["u"],
                     "failure_categories": cats.get("delivery", {}),
                     "note": "generated code volume is not reported as value"},
        "product": {"human_review_queue": reviews, "tool_denials": denied, "failure_categories": cats.get("product", {})},
    }


def render(f) -> str:
    s = snapshot(f)

    def panel(title, d):
        rows = "".join(f"<tr><th>{html.escape(k)}</th><td><code>{html.escape(json.dumps(v))}</code></td></tr>" for k, v in d.items())
        return f"<section><h2>{title}</h2><table>{rows}</table></section>"
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>AI Software Factory — Operations</title>
<style>body{{font:14px system-ui;margin:24px;background:#0f1419;color:#e6e6e6}}h1{{font-weight:600}}
section{{background:#182029;border-radius:8px;padding:16px;margin:12px 0}}h2{{margin:0 0 8px;font-size:16px;color:#7fd1b9}}
table{{border-collapse:collapse;width:100%}}th{{text-align:left;width:32%;color:#9fb3c8;font-weight:500;padding:4px}}td{{padding:4px}}
code{{color:#f0f0f0}}</style><meta http-equiv="refresh" content="10"></head><body>
<h1>AI Software Factory — operations</h1>
{panel("Factory health (control plane)", s["factory"])}
{panel("Delivery outcomes", s["delivery"])}
{panel("Product agent health", s["product"])}
</body></html>"""
