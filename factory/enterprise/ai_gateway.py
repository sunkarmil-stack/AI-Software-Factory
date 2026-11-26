"""Enterprise AI gateway client (e.g. the AI gateway capabilities of Azure API Management).

Layering: the factory's model and tool gateways stay the per-run policy enforcement points
(tiers, bound approvals, idempotency, budgets, evidence). This client is the only egress path
from those gateways to models, MCP servers and business APIs in the production profile. The
enterprise gateway applies bank-wide controls: approved model/tool catalog, Entra
authentication, token and request quotas, content safety and backend credential brokering.

Every request carries the workload's Entra token, W3C trace context and factory correlation
headers so gateway logs join the factory evidence chain.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..core.tracing import current_correlation, current_traceparent


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str, retry_after: float | None = None):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.retry_after = status, code, retry_after


class EnterpriseRevoked(GatewayError):
    """The enterprise gateway disabled this model, MCP server, tool or API (enterprise kill switch)."""


class GatewayThrottled(GatewayError):
    pass


class ContentBlocked(GatewayError):
    pass


class GatewayUnavailable(GatewayError):
    pass


class AIGatewayClient:
    def __init__(self, base_url: str, audience: str, token_provider, models_path: str, mcp_path: str,
                 api_path: str, timeout_s: float = 60.0):
        self.base = base_url.rstrip("/") + "/"
        self.audience, self.tp = audience, token_provider
        self.models_path, self.mcp_path, self.api_path = models_path, mcp_path, api_path
        self.timeout = timeout_s

    # ------------------------------------------------------------ transport
    def _request(self, method: str, path: str, body: Any = None, headers: dict | None = None,
                 correlation: dict | None = None) -> Any:
        h = {"authorization": f"Bearer {self.tp.token(self.audience)}", "content-type": "application/json",
             "accept": "application/json"}
        tp = current_traceparent()
        if tp:
            h["traceparent"] = tp
        for k, v in {**current_correlation(), **(correlation or {})}.items():
            if v:
                h[f"x-factory-{k}"] = str(v)
        h.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path.lstrip("/"), data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                ctype = r.headers.get("content-type", "")
        except urllib.error.HTTPError as e:
            raise self._error(e)
        except OSError as e:
            raise GatewayUnavailable(0, "Unreachable", str(e))
        if "text/event-stream" in ctype:   # MCP streamable HTTP may answer as SSE
            for line in raw.decode().splitlines():
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            return {}
        return json.loads(raw) if raw else {}

    @staticmethod
    def _error(e: urllib.error.HTTPError) -> GatewayError:
        try:
            payload = json.loads(e.read() or b"{}")
        except ValueError:
            payload = {}
        err = payload.get("error", payload) if isinstance(payload, dict) else {}
        code, msg = err.get("code", str(e.code)), err.get("message", e.reason)
        ra = e.headers.get("retry-after")
        if e.code == 429:
            return GatewayThrottled(429, code, msg, float(ra) if ra else None)
        if e.code == 403 and code in ("AssetDisabled", "Revoked", "ApiDisabled"):
            return EnterpriseRevoked(403, code, msg)
        if e.code == 400 and code in ("ContentFiltered", "content_filter", "ContentSafety"):
            return ContentBlocked(400, code, msg)
        if e.code >= 500:
            return GatewayUnavailable(e.code, code, msg)
        return GatewayError(e.code, code, msg)

    # ------------------------------------------------------------ models (OpenAI-compatible endpoint)
    def chat(self, model_alias: str, system: str, user: str, *, temperature: float = 0, max_tokens: int = 4096,
             correlation: dict | None = None) -> tuple[str, dict]:
        r = self._request("POST", self.models_path, {
            "model": model_alias, "temperature": temperature, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
            correlation=correlation)
        return r["choices"][0]["message"]["content"], r.get("usage", {})

    # ------------------------------------------------------------ MCP (streamable HTTP, JSON-RPC 2.0)
    def mcp_transport(self, server: str, correlation: dict | None = None) -> "MCPHttpTransport":
        return MCPHttpTransport(self, self.mcp_path.format(server=server), correlation or {})

    # ------------------------------------------------------------ business APIs
    def call_api(self, api: str, method: str, path: str, body: Any = None, *, idempotency_key: str | None = None,
                 correlation: dict | None = None) -> Any:
        headers = {"idempotency-key": idempotency_key} if idempotency_key else {}
        return self._request(method, self.api_path.format(api=api, path=path), body, headers, correlation)


class MCPHttpTransport:
    def __init__(self, client: AIGatewayClient, path: str, correlation: dict):
        self.c, self.path, self.corr, self._id = client, path, correlation, 0

    def request(self, method: str, params: dict) -> dict:
        self._id += 1
        r = self.c._request("POST", self.path, {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params},
                            headers={"accept": "application/json, text/event-stream"}, correlation=self.corr)
        if "error" in r:
            raise GatewayError(400, str(r["error"].get("code")), r["error"].get("message", ""))
        return r.get("result", {})
