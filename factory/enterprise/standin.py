"""Local stand-in for the Azure services the production profile depends on.

NOT a production component. It lets the production adapters run end to end over real HTTP in
environments without Azure access (CI, this reference build):

  /entra/{tenant}/oauth2/v2.0/token       workload identity federation (client-credentials + JWT assertion)
  /entra/{tenant}/discovery/v2.0/keys     JWKS for token validation
  /kv/keys/{name}[/sign], /kv/secrets/... Key Vault keys (EC P-256, ES256) and secrets, with per-key access
  /aigw/models/chat/completions            AI gateway: OpenAI-compatible model endpoint (model = alias)
  /aigw/mcp/{server}                       AI gateway: MCP servers (JSON-RPC over HTTP)
  /aigw/apis/{api}/...                     AI gateway: business APIs (deposits holds, payments)
  /otlp/v1/traces                          OTLP/HTTP trace receiver
  /admin/*                                 test controls: revoke/restore assets, token limits, request log

Behaviour mirrors the documented contracts closely enough to test the factory's handling of
auth, quotas (429 + Retry-After), content safety (400), enterprise revocation (403) and
correlation headers. It does not emulate APIM policy XML or Entra conditional access.
"""
from __future__ import annotations

import base64
import json
import re
import socket
import threading
import time
from pathlib import Path

import jwt
import uvicorn
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed, decode_dss_signature
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..models.gateway import OFFLINE_HANDLERS
from .keyvault import b64u, ub64u

TENANT = "standin-tenant"
GATEWAY_AUD = "api://enterprise-ai-gateway"
VAULT_AUD = "https://vault.azure.net"
FACTORY_AUD = "api://ai-software-factory"


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class AzureStandIn:
    def __init__(self, port: int | None = None):
        self.port = port or _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.issuer = f"{self.url}/entra/{TENANT}/v2.0"
        self._rsa = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "standin-1"
        self.kv_keys: dict[str, ec.EllipticCurvePrivateKey] = {}
        self.kv_sign_acl: dict[str, set[str]] = {}       # key -> client ids allowed to sign
        self.kv_secrets: dict[str, tuple[str, set[str]]] = {}
        self.models: set[str] = set()
        self.mcp_servers: dict[str, dict[str, dict]] = {}   # server -> tool -> {description, handler}
        self.revoked: set[str] = set()                      # "model:alias", "mcp:server", "api:name"
        self.token_limits: dict[str, int] = {}              # caller -> tokens per minute
        self.usage: dict[str, list[tuple[float, int]]] = {}
        self.blocked_patterns = [re.compile(r"(?i)\bBLOCK-ME-CONTENT\b")]
        self.requests: list[dict] = []
        self.spans: list[dict] = []
        self.holds: dict[str, dict] = {}                     # idempotency key -> hold
        self.ledger: dict[str, dict] = {}
        self._server = None

    # ------------------------------------------------------------ setup helpers
    def add_key(self, name: str, allowed_clients: set[str]) -> None:
        self.kv_keys.setdefault(name, ec.generate_private_key(ec.SECP256R1()))
        self.kv_sign_acl[name] = set(allowed_clients)

    def add_secret(self, name: str, value: str, readers: set[str]) -> None:
        self.kv_secrets[name] = (value, set(readers))

    def write_sa_token(self, client_id: str, path: Path) -> str:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"standin-sa-token:{client_id}", encoding="utf-8")
        return str(path)

    def _issue(self, claims: dict, ttl: int = 3600) -> str:
        now = int(time.time())
        body = {"iss": self.issuer, "iat": now, "nbf": now, "exp": now + ttl, "tid": TENANT, **claims}
        return jwt.encode(body, self._rsa, algorithm="RS256", headers={"kid": self.kid})

    def issue_user_token(self, username: str, roles: list[str], groups: list[str] | None = None,
                         audience: str = FACTORY_AUD, ttl: int = 3600) -> str:
        return self._issue({"aud": audience, "preferred_username": username, "oid": f"oid-{username}",
                            "roles": roles, "groups": groups or [], "idtyp": "user"}, ttl)

    def jwks(self) -> dict:
        n = self._rsa.public_key().public_numbers()
        return {"keys": [{"kty": "RSA", "kid": self.kid, "use": "sig", "alg": "RS256",
                          "n": b64u(n.n.to_bytes((n.n.bit_length() + 7) // 8, "big")),
                          "e": b64u(n.e.to_bytes(3, "big"))}]}

    # ------------------------------------------------------------ auth for stand-in APIs
    def _caller(self, req: Request, audience: str) -> dict:
        tok = (req.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        try:
            return jwt.decode(tok, self._rsa.public_key(), algorithms=["RS256"], audience=audience, issuer=self.issuer)
        except jwt.PyJWTError as e:
            raise PermissionError(str(e))

    def _log(self, req: Request, caller: str, **kw) -> None:
        self.requests.append({"path": req.url.path, "caller": caller, "traceparent": req.headers.get("traceparent"),
                              "run": req.headers.get("x-factory-run"), "actor": req.headers.get("x-factory-actor"),
                              "idempotency_key": req.headers.get("idempotency-key"), **kw})

    @staticmethod
    def _err(status: int, code: str, msg: str, **headers):
        return JSONResponse({"error": {"code": code, "message": msg}}, status, headers=headers or None)

    # ------------------------------------------------------------ app
    def app(self) -> Starlette:
        S = self

        async def token(req: Request):
            form = dict((await req.form()).items())
            assertion = form.get("client_assertion", "")
            if form.get("grant_type") != "client_credentials" or not assertion.startswith("standin-sa-token:"):
                return JSONResponse({"error": "invalid_client"}, 401)
            client = assertion.split(":", 1)[1]
            if client != form.get("client_id"):
                return JSONResponse({"error": "invalid_client", "error_description": "assertion subject mismatch"}, 401)
            aud = form.get("scope", "").removesuffix("/.default")
            return JSONResponse({"token_type": "Bearer", "expires_in": 3600,
                                 "access_token": S._issue({"aud": aud, "azp": client, "sub": client, "appid": client,
                                                           "idtyp": "app", "roles": []})})

        async def keys(req):
            return JSONResponse(S.jwks())

        async def kv_key(req: Request):
            try:
                S._caller(req, VAULT_AUD)
            except PermissionError as e:
                return S._err(401, "Unauthorized", str(e))
            k = S.kv_keys.get(req.path_params["name"])
            if not k:
                return S._err(404, "KeyNotFound", "key not found")
            n = k.public_key().public_numbers()
            return JSONResponse({"key": {"kid": f"{S.url}/kv/keys/{req.path_params['name']}", "kty": "EC", "crv": "P-256",
                                         "x": b64u(n.x.to_bytes(32, "big")), "y": b64u(n.y.to_bytes(32, "big")),
                                         "key_ops": ["sign", "verify"]}})

        async def kv_sign(req: Request):
            try:
                c = S._caller(req, VAULT_AUD)
            except PermissionError as e:
                return S._err(401, "Unauthorized", str(e))
            name = req.path_params["name"]
            if name not in S.kv_keys:
                return S._err(404, "KeyNotFound", "key not found")
            if c.get("azp") not in S.kv_sign_acl.get(name, set()):
                return S._err(403, "Forbidden", f"{c.get('azp')} lacks sign permission on {name}")
            b = await req.json()
            if b.get("alg") != "ES256":
                return S._err(400, "BadParameter", "alg must be ES256 for P-256 keys")
            der = S.kv_keys[name].sign(ub64u(b["value"]), ec.ECDSA(Prehashed(hashes.SHA256())))
            r, s = decode_dss_signature(der)
            S._log(req, c.get("azp"), key=name)
            return JSONResponse({"kid": name, "value": b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))})

        async def kv_secret(req: Request):
            try:
                c = S._caller(req, VAULT_AUD)
            except PermissionError as e:
                return S._err(401, "Unauthorized", str(e))
            v = S.kv_secrets.get(req.path_params["name"])
            if not v or c.get("azp") not in v[1]:
                return S._err(403, "Forbidden", "secret not readable by caller")
            return JSONResponse({"value": v[0]})

        def gateway_caller(req):
            c = S._caller(req, GATEWAY_AUD)
            return c.get("azp") or c.get("preferred_username")

        def over_limit(caller: str, tokens: int) -> bool:
            lim = S.token_limits.get(caller)
            if not lim:
                return False
            window = [(t, n) for t, n in S.usage.get(caller, []) if time.time() - t < 60]
            S.usage[caller] = window
            if sum(n for _, n in window) + tokens > lim:
                return True
            window.append((time.time(), tokens))
            return False

        async def chat(req: Request):
            try:
                caller = gateway_caller(req)
            except PermissionError as e:
                return S._err(401, "Unauthorized", str(e))
            b = await req.json()
            alias = b.get("model")
            S._log(req, caller, model=alias)
            if alias not in S.models:
                return S._err(404, "ModelNotFound", f"{alias} is not in the gateway catalog")
            if f"model:{alias}" in S.revoked:
                return S._err(403, "AssetDisabled", f"model {alias} disabled by enterprise AI governance")
            text = " ".join(m["content"] for m in b.get("messages", []))
            if any(p.search(text) for p in S.blocked_patterns):
                return S._err(400, "ContentFiltered", "request blocked by content safety policy")
            prompt_tokens = len(text) // 4
            if over_limit(caller, prompt_tokens):
                return S._err(429, "TokenLimitExceeded", "token rate limit exceeded", **{"Retry-After": "7"})
            task = json.loads(b["messages"][-1]["content"])
            out = json.dumps(OFFLINE_HANDLERS[task["kind"]](task))   # stand-in "model"
            return JSONResponse({"id": "chatcmpl-standin", "model": alias, "choices": [
                {"index": 0, "message": {"role": "assistant", "content": out}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": len(out) // 4}})

        async def mcp(req: Request):
            try:
                caller = gateway_caller(req)
            except PermissionError as e:
                return S._err(401, "Unauthorized", str(e))
            server = req.path_params["server"]
            b = await req.json()
            S._log(req, caller, mcp=server, method=b.get("method"))
            if f"mcp:{server}" in S.revoked:
                return S._err(403, "AssetDisabled", f"MCP server {server} disabled")
            tools = S.mcp_servers.get(server)
            if tools is None:
                return S._err(404, "NotFound", "unknown MCP server")
            m, rid = b.get("method"), b.get("id")
            if m == "initialize":
                res = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                       "serverInfo": {"name": server}, "instructions": "Gateway-published MCP server"}
            elif m == "tools/list":
                res = {"tools": [{"name": n, "description": t["description"], "inputSchema": {"type": "object"}}
                                 for n, t in tools.items()]}
            elif m == "tools/call":
                t = tools.get(b["params"]["name"])
                if not t:
                    return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": "unknown tool"}})
                out = t["handler"](b["params"].get("arguments", {}))
                res = {"structuredContent": out, "content": [{"type": "text", "text": json.dumps(out)}]}
            else:
                return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "method not found"}})
            return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": res})

        async def api(req: Request):
            try:
                caller = gateway_caller(req)
            except PermissionError as e:
                return S._err(401, "Unauthorized", str(e))
            name, path = req.path_params["api"], "/" + req.path_params["path"]
            S._log(req, caller, api=name, method=req.method)
            if f"api:{name}" in S.revoked:
                return S._err(403, "ApiDisabled", f"API {name} disabled by enterprise governance")
            key = req.headers.get("idempotency-key")
            if (name, path, req.method) == ("deposits", "/holds", "POST"):
                b = await req.json()
                if key and key in S.holds:
                    return JSONResponse(S.holds[key])
                h = {"hold_id": f"HLD-SOR-{len(S.holds) + 1}", "status": "placed", "account": b.get("subject_id"),
                     "amount_cents": b.get("amount_cents"), "operation_id": key}
                S.holds[key or h["hold_id"]] = h
                return JSONResponse(h, 201)
            if (name, path, req.method) == ("deposits", "/holds", "GET"):
                h = S.holds.get(req.query_params.get("operation_id", ""))
                return JSONResponse(h) if h else S._err(404, "NotFound", "no hold for operation")
            if (name, path, req.method) == ("payments", "/ledger", "POST"):
                b = await req.json()
                if key and key in S.ledger:
                    return JSONResponse(S.ledger[key])
                e = {"ledger_id": f"LED-SOR-{len(S.ledger) + 1}", "status": "committed"}
                S.ledger[key or e["ledger_id"]] = e
                return JSONResponse(e, 201)
            if (name, path, req.method) == ("payments", "/ledger", "GET"):
                e = S.ledger.get(req.query_params.get("operation_id", ""))
                return JSONResponse(e) if e else S._err(404, "NotFound", "no entry")
            return S._err(404, "NotFound", f"{req.method} {name}{path}")

        async def otlp(req: Request):
            b = await req.json()
            for rs in b.get("resourceSpans", []):
                for ss in rs.get("scopeSpans", []):
                    S.spans += ss.get("spans", [])
            return JSONResponse({})

        async def admin(req: Request):
            b = await req.json() if req.method == "POST" else {}
            op = req.path_params["op"]
            if op == "revoke":
                S.revoked.add(b["asset"])
            elif op == "restore":
                S.revoked.discard(b["asset"])
            elif op == "limit":
                S.token_limits[b["caller"]] = int(b["tokens_per_minute"])
            return JSONResponse({"revoked": sorted(S.revoked), "requests": len(S.requests)})

        return Starlette(routes=[
            Route(f"/entra/{TENANT}/oauth2/v2.0/token", token, methods=["POST"]),
            Route(f"/entra/{TENANT}/discovery/v2.0/keys", keys),
            Route("/kv/keys/{name}", kv_key), Route("/kv/keys/{name}/sign", kv_sign, methods=["POST"]),
            Route("/kv/secrets/{name}", kv_secret),
            Route("/aigw/models/chat/completions", chat, methods=["POST"]),
            Route("/aigw/mcp/{server}", mcp, methods=["POST"]),
            Route("/aigw/apis/{api}/{path:path}", api, methods=["GET", "POST"]),
            Route("/otlp/v1/traces", otlp, methods=["POST"]),
            Route("/admin/{op}", admin, methods=["GET", "POST"]),
        ])

    # ------------------------------------------------------------ lifecycle
    def start(self) -> "AzureStandIn":
        self._server = uvicorn.Server(uvicorn.Config(self.app(), host="127.0.0.1", port=self.port, log_level="error"))
        threading.Thread(target=self._server.run, daemon=True).start()
        for _ in range(200):
            if self._server.started:
                break
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True

    # ------------------------------------------------------------ a ready-made production config
    def production_settings(self, home: Path, **extra) -> dict:
        """Settings dict for the production profile pointed at this stand-in."""
        token_file = self.write_sa_token("factory-workload", Path(home) / "sa-token")
        for ident, key in {"ci-builder": "factory-ci-builder", "release-service": "factory-release",
                           "skill-registry": "factory-skill-registry", "approval-service": "factory-approval",
                           "policy-service": "factory-policy"}.items():
            self.add_key(key, {"factory-workload"})
        self.add_secret("prod-db-password", "kv-held-secret-value", {"factory-workload"})
        self.models |= {"claude-delivery", "claude-review", "gpt-delivery", "rag-assistant"}
        cfg = {
            "profile": "production", "auth": "entra", "signer": "keyvault", "secrets": "keyvault",
            "models_catalog": "models.production.yaml", "deployment_target": "gitops", "execution": "queue",
            "sandbox_backend": "kubernetes", "sandbox_runtime_class": "kata-vm-isolation",
            "otel_endpoint": f"{self.url}/otlp",
            "ai_gateway": {"base_url": f"{self.url}/aigw/", "audience": GATEWAY_AUD},
            "entra": {"tenant_id": TENANT, "authority_host": f"{self.url}/entra", "issuer": self.issuer,
                      "audience": FACTORY_AUD, "jwks_url": f"{self.url}/entra/{TENANT}/discovery/v2.0/keys",
                      "role_map": {"Factory.ReleaseOwner": "release_owner", "Factory.ChangeManager": "change_manager",
                                   "Factory.Auditor": "auditor", "Factory.Operator": "operator",
                                   "Factory.ProductOwner": "product_owner", "Factory.Architect": "architect",
                                   "Factory.Engineer": "engineer", "Factory.Supervisor": "supervisor",
                                   "Factory.PlatformAdmin": "platform_admin",
                                   "Factory.Advisor": "advisor", "Factory.PortfolioManager": "portfolio_manager",
                                   "Factory.ComplianceOfficer": "compliance_officer"},
                      "tenant_map": {"grp-retail": "retail", "grp-wealth": "wealth"}},
            "workload": {"client_id": "factory-workload", "tenant_id": TENANT, "token_file": token_file,
                         "authority_host": f"{self.url}/entra"},
            "keyvault": {"url": f"{self.url}/kv", "audience": VAULT_AUD},
        }
        cfg.update(extra)
        return cfg
