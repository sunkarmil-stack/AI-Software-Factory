"""FastAPI adapter (template: fastapi-service). Business logic lives in app.routes."""
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
