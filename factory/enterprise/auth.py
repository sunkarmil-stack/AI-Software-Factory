"""Microsoft Entra ID integration.

WorkloadTokenProvider : AKS workload identity federation. Exchanges the projected service-account
                        token (AZURE_FEDERATED_TOKEN_FILE) for an Entra access token with the
                        client-credentials grant and a JWT-bearer client assertion; tokens are cached
                        until shortly before expiry.
EntraValidator        : validates bearer tokens on the factory API (RS256 signature from JWKS,
                        issuer, audience, expiry, not-before) and maps app roles / groups to factory
                        roles and tenants. Dev tokens are not accepted in the production profile.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
import urllib.request

import jwt

from ..core.identity import Directory, HUMAN_ROLES, Principal


class AuthError(PermissionError):
    pass


PG_AAD_AUDIENCE = "https://ossrdbms-aad.database.windows.net"   # Azure Database for PostgreSQL Entra auth


class WorkloadTokenProvider:
    def __init__(self, client_id: str, tenant_id: str, token_file: str, authority_host: str):
        self.client_id, self.tenant_id, self.token_file = client_id, tenant_id, token_file
        self.authority = authority_host.rstrip("/")
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def token(self, audience: str) -> str:
        scope = audience.rstrip("/") + "/.default"
        with self._lock:
            hit = self._cache.get(scope)
            if hit and hit[1] - 300 > time.time():
                return hit[0]
        assertion = open(self.token_file).read().strip()
        body = urllib.parse.urlencode({
            "client_id": self.client_id, "scope": scope, "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion}).encode()
        req = urllib.request.Request(f"{self.authority}/{self.tenant_id}/oauth2/v2.0/token", data=body,
                                     headers={"content-type": "application/x-www-form-urlencoded"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise AuthError(f"workload token request failed: {e.code} {e.read()[:200]!r}")
        tok = data["access_token"]
        with self._lock:
            self._cache[scope] = (tok, time.time() + float(data.get("expires_in", 3600)))
        return tok


class EntraValidator:
    def __init__(self, directory: Directory, issuer: str, audience: str, jwks_url: str,
                 principal_claim: str = "preferred_username", role_map: dict | None = None,
                 tenant_map: dict | None = None):
        self.dir = directory
        self.issuer, self.audience = issuer, audience
        self.principal_claim = principal_claim
        self.role_map = role_map or {}
        self.tenant_map = tenant_map or {}
        self._jwks = jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)

    def principal(self, token: str) -> Principal:
        try:
            key = self._jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=["RS256"], audience=self.audience, issuer=self.issuer,
                                options={"require": ["exp", "iat", "iss", "aud"]}, leeway=60)
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid token: {e}")
        app = claims.get("idtyp") == "app"
        pid = claims.get("azp") if app else claims.get(self.principal_claim) or claims.get("oid")
        if not pid:
            raise AuthError("token has no principal claim")
        roles = {self.role_map.get(r, r) for r in claims.get("roles", [])}
        roles &= HUMAN_ROLES if not app else roles
        tenants = {self.tenant_map[g] for g in claims.get("groups", []) if g in self.tenant_map}
        tenants |= set(claims.get("factory_tenants", []))
        existing = self.dir.principals.get(pid)
        if app:
            if not existing or existing.kind == "human":
                raise AuthError(f"application {pid} is not a registered factory workload")
            return existing
        # just-in-time provisioning: Entra is the source of truth for human roles; approvals re-check at use
        p = Principal(pid, "human", roles, tenants or (existing.tenants if existing else set()), [], None)
        self.dir.principals[pid] = p
        return p
