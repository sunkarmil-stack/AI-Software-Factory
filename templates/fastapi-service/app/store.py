"""Synthetic, tenant-scoped data store (delivery uses synthetic data only — Boundary A)."""
from __future__ import annotations

import copy

SEED = {
    "acc-100": {"id": "acc-100", "tenant": "retail", "owner": "user-a", "balance_cents": 125000, "currency": "CAD"},
    "acc-101": {"id": "acc-101", "tenant": "retail", "owner": "user-b", "balance_cents": 990, "currency": "CAD"},
    "acc-900": {"id": "acc-900", "tenant": "wealth", "owner": "user-z", "balance_cents": 5000000, "currency": "USD"},
}


class Store:
    def __init__(self):
        self.items = copy.deepcopy(SEED)
        self.children: dict[str, list[dict]] = {}

    def get(self, tenant: str, item_id: str) -> dict | None:
        it = self.items.get(item_id)
        return it if it and it["tenant"] == tenant else None  # no cross-tenant existence leak

    def list(self, tenant: str) -> list[dict]:
        return [v for v in self.items.values() if v["tenant"] == tenant]

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
