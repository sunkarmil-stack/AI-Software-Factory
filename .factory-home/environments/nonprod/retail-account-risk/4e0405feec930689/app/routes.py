"""GENERATED from SPEC-RISK-CASES-API@v1 contract CON-CASES@1.
Regenerate via the factory; manual edits must go through review."""
from .http import HTTPError, Route
from .security import require_scope
from .store import STORE


def list_cases(req):
    c = require_scope(req, "cases:read")
    return 200, {"items": [{k: v for k, v in it.items() if k not in ['customer_id']} for it in STORE.list(c.tenant, "cases")]}


def get_case(req):
    c = require_scope(req, "cases:read")
    item = STORE.get(c.tenant, req.params["case_id"], "cases")
    if item is None:
        raise HTTPError(404, "not found")
    item = {k: v for k, v in item.items() if k not in ['customer_id']}
    return 200, item


def add_case_note(req):
    c = require_scope(req, "cases:write")
    item = STORE.get(c.tenant, req.params["case_id"], "cases")
    if item is None:
        raise HTTPError(404, "not found")
    body = req.body if isinstance(req.body, dict) else {}
    if item.get("status") != 'open':
        raise HTTPError(409, "status must be open")
    if "note" not in body:
        raise HTTPError(400, "missing note")
    if "author" not in body:
        raise HTTPError(400, "missing author")
    if "author" in body and not isinstance(body["author"], str):
        raise HTTPError(400, "author must be a string")
    if "idempotency_key" in body and not isinstance(body["idempotency_key"], str):
        raise HTTPError(400, "idempotency_key must be a string")
    if "note" in body and not isinstance(body["note"], str):
        raise HTTPError(400, "note must be a string")
    if "note" in body and len(str(body["note"])) < 10:
        raise HTTPError(400, "note too short")
    return 201, STORE.add_child(c.tenant, item["id"], "notes", body)


def healthz(req):
    return 200, {"status": "ok"}

ROUTES = [
    Route("GET", "/cases", "listCases", list_cases, "cases:read"),
    Route("GET", "/cases/{case_id}", "getCase", get_case, "cases:read"),
    Route("POST", "/cases/{case_id}/notes", "addCaseNote", add_case_note, "cases:write"),
    Route("GET", "/healthz", "healthz", healthz, "public"),
]
