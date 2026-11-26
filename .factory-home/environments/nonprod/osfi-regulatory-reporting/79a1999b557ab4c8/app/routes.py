"""GENERATED from SPEC-REGULATORY-RETURNS-API@v1 contract CON-RETURNS@1.
Regenerate via the factory; manual edits must go through review."""
from .http import HTTPError, Route
from .security import require_scope
from .store import STORE


def list_returns(req):
    c = require_scope(req, "returns:read")
    return 200, {"items": [{k: v for k, v in it.items() if k not in ['reviewer_notes']} for it in STORE.list(c.tenant, "returns")]}


def get_return(req):
    c = require_scope(req, "returns:read")
    item = STORE.get(c.tenant, req.params["return_id"], "returns")
    if item is None:
        raise HTTPError(404, "not found")
    item = {k: v for k, v in item.items() if k not in ['reviewer_notes']}
    return 200, item


def record_filing_receipt(req):
    c = require_scope(req, "returns:file", require_mfa=True)
    item = STORE.get(c.tenant, req.params["return_id"], "returns")
    if item is None:
        raise HTTPError(404, "not found")
    body = req.body if isinstance(req.body, dict) else {}
    if item.get("status") != 'certified':
        raise HTTPError(409, "status must be certified")
    if "receipt_ref" not in body:
        raise HTTPError(400, "missing receipt_ref")
    if "filed_by" not in body:
        raise HTTPError(400, "missing filed_by")
    if "filed_by" in body and not isinstance(body["filed_by"], str):
        raise HTTPError(400, "filed_by must be a string")
    if "idempotency_key" in body and not isinstance(body["idempotency_key"], str):
        raise HTTPError(400, "idempotency_key must be a string")
    if "receipt_ref" in body and not isinstance(body["receipt_ref"], str):
        raise HTTPError(400, "receipt_ref must be a string")
    if "receipt_ref" in body and len(str(body["receipt_ref"])) < 8:
        raise HTTPError(400, "receipt_ref too short")
    return 201, STORE.add_child(c.tenant, item["id"], "receipts", body)


def healthz(req):
    return 200, {"status": "ok"}

ROUTES = [
    Route("GET", "/returns", "listReturns", list_returns, "returns:read"),
    Route("GET", "/returns/{return_id}", "getReturn", get_return, "returns:read"),
    Route("POST", "/returns/{return_id}/receipts", "recordFilingReceipt", record_filing_receipt, "returns:file"),
    Route("GET", "/healthz", "healthz", healthz, "public"),
]
