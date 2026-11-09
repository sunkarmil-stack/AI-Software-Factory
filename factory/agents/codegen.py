"""Deterministic template code generation for the FastAPI service template.

Used (a) by the offline model provider so the full governed pipeline runs without model
access, and (b) by QA tooling to materialize protected acceptance tests from approved
acceptance criteria. With a live model deployment, the implementation agent sends the
same task and must return the same file-map schema.
"""
from __future__ import annotations

import json
import re


def _snake(s: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()


def handler_source(ep: dict) -> str:
    fn = _snake(ep["operation_id"])
    pid = re.findall(r"\{(\w+)\}", ep["path"])
    lines = [f"def {fn}(req):"]
    if ep["required_scope"] == "public":
        lines.append('    return 200, {"status": "ok"}')
        return "\n".join(lines)
    mfa = ", require_mfa=True" if ep.get("require_mfa") else ""
    lines.append(f'    c = require_scope(req, "{ep["required_scope"]}"{mfa})')
    kind = ep.get("kind", "get")
    if kind == "list":
        lines.append('    return 200, {"items": STORE.list(c.tenant)}')
    elif kind == "get":
        lines += [f'    item = STORE.get(c.tenant, req.params["{pid[-1]}"])',
                  '    if item is None:', '        raise HTTPError(404, "not found")']
        hidden = ep.get("hide_fields") or []
        if hidden:
            lines.append(f"    item = {{k: v for k, v in item.items() if k not in {hidden!r}}}")
        lines.append("    return 200, item")
    elif kind == "create_child":
        schema = ep.get("request_schema", {})
        lines += [f'    item = STORE.get(c.tenant, req.params["{pid[-1]}"])',
                  '    if item is None:', '        raise HTTPError(404, "not found")',
                  '    body = req.body if isinstance(req.body, dict) else {}']
        for f in schema.get("required", []):
            lines += [f'    if "{f}" not in body:', f'        raise HTTPError(400, "missing {f}")']
        for f, spec in schema.get("properties", {}).items():
            if spec.get("type") == "integer":
                lines += [f'    if "{f}" in body and not isinstance(body["{f}"], int):',
                          f'        raise HTTPError(400, "{f} must be an integer")']
            if "minimum" in spec:
                lines += [f'    if "{f}" in body and body["{f}"] < {spec["minimum"]}:',
                          f'        raise HTTPError(400, "{f} below minimum")']
            if "maximum" in spec:
                lines += [f'    if "{f}" in body and body["{f}"] > {spec["maximum"]}:',
                          f'        raise HTTPError(400, "{f} above maximum")']
        lines.append(f'    return 201, STORE.add_child(c.tenant, item["id"], "{ep.get("child_kind", "children")}", body)')
    return "\n".join(lines)


def generate_service(spec: dict, contract: dict) -> dict[str, str]:
    eps = contract["endpoints"]
    if not any(e["path"] == "/healthz" for e in eps):
        eps = eps + [{"operation_id": "healthz", "method": "GET", "path": "/healthz", "required_scope": "public",
                      "responses": {"200": "ok"}}]
    routes = ['"""GENERATED from ' + f'{spec["spec_id"]}@v{spec["version"]} contract {contract["id"]}@{contract["version"]}.',
              'Regenerate via the factory; manual edits must go through review."""',
              "from .http import HTTPError, Route", "from .security import require_scope", "from .store import STORE", ""]
    for ep in eps:
        routes += ["", handler_source(ep), ""]
    routes.append("ROUTES = [")
    for ep in eps:
        routes.append(f'    Route("{ep["method"].upper()}", "{ep["path"]}", "{ep["operation_id"]}", '
                      f'{_snake(ep["operation_id"])}, "{ep["required_scope"]}"),')
    routes.append("]")
    openapi = {"openapi": "3.1.0", "info": {"title": spec["title"], "version": str(contract["version"])}, "paths": {}}
    for ep in eps:
        op = {"operationId": ep["operation_id"], "x-required-scope": ep["required_scope"],
              "x-require-mfa": bool(ep.get("require_mfa")),
              "responses": {str(k): {"description": str(v)} for k, v in ep["responses"].items()}}
        if ep.get("request_schema"):
            op["requestBody"] = {"content": {"application/json": {"schema": ep["request_schema"]}}}
        openapi["paths"].setdefault(ep["path"], {})[ep["method"].lower()] = op
    main = '''"""FastAPI adapter (template: fastapi-service). Business logic lives in app.routes."""
import json
import pathlib

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .http import dispatch
from .routes import ROUTES

OPENAPI = json.loads((pathlib.Path(__file__).parent.parent / "openapi.json").read_text())
api = FastAPI(title=OPENAPI["info"]["title"], version=OPENAPI["info"]["version"])
api.openapi = lambda: OPENAPI


@api.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
async def entry(full_path: str, request: Request):
    raw = await request.body()
    body = json.loads(raw) if raw else None
    status, payload = dispatch(ROUTES, request.method, "/" + full_path, dict(request.headers), body)
    return JSONResponse(payload, status_code=status)
'''
    return {"app/routes.py": "\n".join(routes) + "\n", "app/main.py": main,
            "openapi.json": json.dumps(openapi, indent=2, sort_keys=True) + "\n"}


def generate_tests(spec: dict, header: str, prefix: str = "test") -> str:
    out = [f'"""{header} — from {spec["spec_id"]}@v{spec["version"]}."""', ""]
    for r in spec["requirements"]:
        for ac in r["acceptance_criteria"]:
            rq, ex = ac["check"]["request"], ac["check"]["expect"]
            name = f"{prefix}_{ac['id'].replace('-', '_').lower()}"
            out += [f"def {name}(call):",
                    f'    """{r["id"]} / {ac["id"]}: GIVEN {ac["given"]} WHEN {ac["when"]} THEN {ac["then"]}"""',
                    f"    status, body = call({rq['method']!r}, {rq['path']!r}, {rq.get('headers', {})!r}, {rq.get('body')!r})",
                    f"    assert status == {ex['status']}, body"]
            for k, v in (ex.get("body_contains") or {}).items():
                out.append(f"    assert body.get({k!r}) == {v!r}, body")
            for k in ex.get("body_excludes") or []:
                out.append(f"    assert {k!r} not in body, body")
            if "items_len" in ex:
                out.append(f"    assert len(body['items']) == {ex['items_len']}, body")
            out.append("")
    return "\n".join(out)
