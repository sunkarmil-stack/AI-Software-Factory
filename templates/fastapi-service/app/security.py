"""Authorization helpers. Claims arrive from the enterprise API gateway after token
validation; the service still enforces object-level authorization itself."""
from __future__ import annotations

from dataclasses import dataclass

from .http import HTTPError, Request


@dataclass
class Caller:
    subject: str
    tenant: str
    scopes: set[str]
    mfa: bool


def caller(req: Request) -> Caller:
    sub, tenant = req.header("X-Principal"), req.header("X-Tenant")
    if not sub or not tenant:
        raise HTTPError(401, "unauthenticated")
    scopes = set((req.header("X-Scopes") or "").split())
    return Caller(sub, tenant, scopes, (req.header("X-MFA") or "").lower() == "true")


def require_scope(req: Request, scope: str, require_mfa: bool = False) -> Caller:
    c = caller(req)
    if scope not in c.scopes:
        raise HTTPError(403, f"missing scope {scope}")
    if require_mfa and not c.mfa:
        raise HTTPError(403, "step-up authentication required")
    return c
