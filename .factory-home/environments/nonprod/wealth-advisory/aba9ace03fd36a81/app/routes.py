"""GENERATED from SPEC-WEALTH-ADVICE-API@v1 contract CON-ADVICE@1.
Regenerate via the factory; manual edits must go through review."""
from .http import HTTPError, Route
from .security import require_scope
from .store import STORE


def get_account(req):
    c = require_scope(req, "advice:read")
    item = STORE.get(c.tenant, req.params["account_id"])
    if item is None:
        raise HTTPError(404, "not found")
    return 200, item


def record_recommendation(req):
    c = require_scope(req, "advice:write", require_mfa=True)
    item = STORE.get(c.tenant, req.params["account_id"])
    if item is None:
        raise HTTPError(404, "not found")
    body = req.body if isinstance(req.body, dict) else {}
    if "product_code" not in body:
        raise HTTPError(400, "missing product_code")
    if "side" not in body:
        raise HTTPError(400, "missing side")
    if "amount_cents" not in body:
        raise HTTPError(400, "missing amount_cents")
    if "rationale" not in body:
        raise HTTPError(400, "missing rationale")
    if "amount_cents" in body and not isinstance(body["amount_cents"], int):
        raise HTTPError(400, "amount_cents must be an integer")
    if "amount_cents" in body and body["amount_cents"] < 1:
        raise HTTPError(400, "amount_cents below minimum")
    if "amount_cents" in body and body["amount_cents"] > 1000000000:
        raise HTTPError(400, "amount_cents above maximum")
    if "idempotency_key" in body and not isinstance(body["idempotency_key"], str):
        raise HTTPError(400, "idempotency_key must be a string")
    if "product_code" in body and not isinstance(body["product_code"], str):
        raise HTTPError(400, "product_code must be a string")
    if "rationale" in body and not isinstance(body["rationale"], str):
        raise HTTPError(400, "rationale must be a string")
    if "side" in body and not isinstance(body["side"], str):
        raise HTTPError(400, "side must be a string")
    return 201, STORE.add_child(c.tenant, item["id"], "recommendations", body)


def healthz(req):
    return 200, {"status": "ok"}

ROUTES = [
    Route("GET", "/accounts/{account_id}", "getAccount", get_account, "advice:read"),
    Route("POST", "/accounts/{account_id}/recommendations", "recordRecommendation", record_recommendation, "advice:write"),
    Route("GET", "/healthz", "healthz", healthz, "public"),
]
