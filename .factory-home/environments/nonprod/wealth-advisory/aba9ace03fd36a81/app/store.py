"""Synthetic, tenant-scoped data store (delivery uses synthetic data only — Boundary A).

Resources are named collections. Specs pick one per endpoint with `resource:` (default `accounts`),
so the same generated handlers serve accounts, regulatory returns or any collection added here.
"""
from __future__ import annotations

import copy

SEED = {
    "acc-100": {"id": "acc-100", "tenant": "retail", "owner": "user-a", "balance_cents": 125000, "currency": "CAD"},
    "acc-101": {"id": "acc-101", "tenant": "retail", "owner": "user-b", "balance_cents": 990, "currency": "CAD"},
    "acc-900": {"id": "acc-900", "tenant": "wealth", "owner": "user-z", "balance_cents": 5000000, "currency": "USD"},
}

# Synthetic regulatory returns (tenant finance). Values are invented for delivery tests only.
RETURNS = {
    "LR-2026-07-31": {"id": "LR-2026-07-31", "tenant": "finance", "return_code": "LR", "period_end": "2026-07-31",
                      "status": "filed", "headline_pct": 4.303, "requirement_pct": 3.5,
                      "snapshot_id": "SNAP-synthetic-lr-q3", "reviewer_notes": "internal only"},
    "LR-2026-10-31": {"id": "LR-2026-10-31", "tenant": "finance", "return_code": "LR", "period_end": "2026-10-31",
                      "status": "certified", "headline_pct": 4.28, "requirement_pct": 3.5,
                      "snapshot_id": "SNAP-synthetic-lr-q4", "reviewer_notes": "internal only"},
    "LA-2026-10-31": {"id": "LA-2026-10-31", "tenant": "finance", "return_code": "LA", "period_end": "2026-10-31",
                      "status": "certified", "headline_pct": 133.333, "requirement_pct": 100.0,
                      "snapshot_id": "SNAP-synthetic-la-oct", "reviewer_notes": "internal only"},
}

# Synthetic risk cases (tenant retail, plus one wealth case for isolation tests). Invented for delivery tests only.
CASES = {
    "CASE-1001": {"id": "CASE-1001", "tenant": "retail", "case_type": "fraud", "status": "open", "score": 100,
                  "reason_codes": "VELOCITY,GEO_IMPOSSIBLE,AMOUNT_SPIKE", "customer_id": "cust-b"},
    "CASE-1002": {"id": "CASE-1002", "tenant": "retail", "case_type": "credit", "status": "open", "score": 80,
                  "reason_codes": "DPD_30,HIGH_UTILIZATION,SCORE_DROP", "customer_id": "cust-c"},
    "CASE-1003": {"id": "CASE-1003", "tenant": "retail", "case_type": "aml", "status": "referred", "score": 0,
                  "reason_codes": "CASH_24H_THRESHOLD,STRUCTURING_PATTERN", "customer_id": "cust-d"},
    "CASE-W-01": {"id": "CASE-W-01", "tenant": "wealth", "case_type": "fraud", "status": "open", "score": 55,
                  "reason_codes": "NEW_DEVICE", "customer_id": "cust-w"},
}

COLLECTIONS = {"accounts": SEED, "returns": RETURNS, "cases": CASES}


class Store:
    def __init__(self):
        self.collections = copy.deepcopy(COLLECTIONS)
        self.items = self.collections["accounts"]          # backwards compatible view
        self.children: dict[str, list[dict]] = {}

    def get(self, tenant: str, item_id: str, resource: str = "accounts") -> dict | None:
        it = self.collections.get(resource, {}).get(item_id)
        return it if it and it["tenant"] == tenant else None  # no cross-tenant existence leak

    def list(self, tenant: str, resource: str = "accounts") -> list[dict]:
        return [v for v in self.collections.get(resource, {}).values() if v["tenant"] == tenant]

    def add_child(self, tenant: str, item_id: str, kind: str, child: dict) -> dict:
        key = f"{item_id}/{kind}"
        rows = self.children.setdefault(key, [])
        ik = child.get("idempotency_key")
        for r in rows:
            if ik and r.get("idempotency_key") == ik:
                return r
        child = {**child, "id": f"{kind[:-1] if kind.endswith('s') else kind}-{len(rows) + 1}", "parent": item_id}
        rows.append(child)
        return child


STORE = Store()
