"""Framework-neutral HTTP core. FastAPI (app/main.py) is a thin adapter over `dispatch`,
so handlers, contract tests and acceptance tests run identically with or without FastAPI."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable


class HTTPError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


@dataclass
class Request:
    method: str
    path: str
    headers: dict = field(default_factory=dict)
    body: Any = None
    params: dict = field(default_factory=dict)

    def header(self, name: str, default: str | None = None) -> str | None:
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return default


@dataclass
class Route:
    method: str
    path: str
    operation_id: str
    handler: Callable[[Request], tuple[int, Any]]
    required_scope: str

    def match(self, method: str, path: str) -> dict | None:
        if method.upper() != self.method:
            return None
        pattern = "^" + re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", self.path) + "$"
        m = re.match(pattern, path)
        return m.groupdict() if m else None


def dispatch(routes: list[Route], method: str, path: str, headers: dict | None = None, body: Any = None) -> tuple[int, Any]:
    for r in routes:
        params = r.match(method, path)
        if params is not None:
            req = Request(method.upper(), path, headers or {}, body, params)
            try:
                return r.handler(req)
            except HTTPError as e:
                return e.status, {"error": e.detail}
    return 404, {"error": "route not found"}
