"""MCP integration (REQ 020).

- Only servers in the approved registry (identity + pinned protocol versions) connect.
- Unsupported protocol versions fail safely (no connection).
- Server-provided tool descriptions and instructions are stored as *untrusted metadata*;
  they never create, widen or describe authorization. Only tools that have an approved
  factory contract are exposed, and every call still goes through ToolGateway.
"""
from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Any, Protocol

from .gateway import CallContext, ToolGateway, ToolImpl, PermanentError

SUPPORTED_PROTOCOL_VERSIONS = ["2025-06-18", "2025-11-25"]


class MCPTransport(Protocol):
    def request(self, method: str, params: dict) -> dict: ...


class MCPRejected(ConnectionError):
    pass


@dataclass
class ApprovedServer:
    name: str
    identity_fingerprint: str
    protocol_versions: list[str]
    contracts: dict[str, dict]  # mcp tool name -> factory contract (approved separately)


class MCPAdapter:
    def __init__(self, gateway: ToolGateway, approved: dict[str, ApprovedServer]):
        self.gw, self.approved = gateway, approved
        self.untrusted_metadata: dict[str, Any] = {}

    def connect(self, name: str, transport: MCPTransport, presented_fingerprint: str) -> list[str]:
        srv = self.approved.get(name)
        if not srv:
            raise MCPRejected(f"MCP server {name} is not an approved server identity")
        if not hmac.compare_digest(srv.identity_fingerprint, presented_fingerprint):
            raise MCPRejected(f"MCP server {name} identity mismatch")
        versions = [v for v in srv.protocol_versions if v in SUPPORTED_PROTOCOL_VERSIONS]
        init = transport.request("initialize", {"protocolVersion": versions[-1] if versions else "none",
                                                "capabilities": {}, "clientInfo": {"name": "ai-software-factory"}})
        negotiated = init.get("protocolVersion")
        if negotiated not in versions:
            raise MCPRejected(f"unsupported MCP protocol version {negotiated!r}; pinned {versions}")
        self.untrusted_metadata[name] = {"instructions": init.get("instructions")}
        listed = transport.request("tools/list", {}).get("tools", [])
        exposed = []
        for t in listed:
            self.untrusted_metadata.setdefault(f"{name}/tools", {})[t["name"]] = t.get("description")
            contract = srv.contracts.get(t["name"])
            if not contract:
                continue  # listed but not approved: ignored
            def fn(action: str, args: dict, ctx: CallContext, _t=t["name"], _tr=transport) -> dict:
                r = _tr.request("tools/call", {"name": _t, "arguments": args})
                if r.get("isError"):
                    raise PermanentError(str(r.get("content")))
                return r.get("structuredContent") or {}
            self.gw.register(contract, ToolImpl(fn))
            exposed.append(contract["name"])
        return exposed


class MCPToolset:
    """One governed factory contract backed by several tools on an approved MCP server.

    The contract maps each factory action to one MCP tool (`endpoint.tools`). The connection is made on
    first use: server identity and protocol version are checked, and every mapped tool's input schema must
    match the digest pinned at approval time. A changed tool definition is refused until re-approved.
    Tools the server lists but the contract does not map are never exposed; their descriptions are kept
    only as untrusted metadata. Every call still passes the tool gateway (authorization, tiers, evidence).
    """

    def __init__(self, server_name: str, server_cfg: dict, transport_factory, ledger=None):
        self.name, self.cfg, self.factory, self.ledger = server_name, server_cfg, transport_factory, ledger
        self.transport = None
        self.untrusted_metadata: dict[str, Any] = {}

    def _connect(self):
        from ..core.util import digest_obj
        tr = self.factory()
        presented = getattr(tr, "fingerprint", None)
        if presented is not None and not hmac.compare_digest(self.cfg["identity_fingerprint"], presented):
            raise MCPRejected(f"MCP server {self.name} identity mismatch")
        versions = [v for v in self.cfg["protocol_versions"] if v in SUPPORTED_PROTOCOL_VERSIONS]
        init = tr.request("initialize", {"protocolVersion": versions[-1] if versions else "none", "capabilities": {},
                                         "clientInfo": {"name": "ai-software-factory"}})
        if init.get("protocolVersion") not in versions:
            raise MCPRejected(f"unsupported MCP protocol version {init.get('protocolVersion')!r}; pinned {versions}")
        listed = {t["name"]: t for t in tr.request("tools/list", {}).get("tools", [])}
        self.untrusted_metadata = {"instructions": init.get("instructions"),
                                   "tools": {n: t.get("description") for n, t in listed.items()}}
        for tool, pin in (self.cfg.get("pinned_tools") or {}).items():
            if tool not in listed:
                raise MCPRejected(f"approved tool {tool} is no longer offered by {self.name}")
            if digest_obj(listed[tool].get("inputSchema") or {}) != pin:
                if self.ledger:
                    self.ledger.record("mcp.tool_changed", f"{self.name}/{tool}", "mcp-adapter",
                                       {"pinned": pin, "presented": digest_obj(listed[tool].get("inputSchema") or {})})
                raise MCPRejected(f"tool {tool} on {self.name} changed since approval; re-approval required")
        self.transport = tr

    def impl_for(self, contract: dict) -> ToolImpl:
        mapping = contract["endpoint"]["tools"]
        unpinned = [t for t in mapping.values() if t not in (self.cfg.get("pinned_tools") or {})]
        if unpinned:
            raise MCPRejected(f"contract {contract['name']} maps tools with no approved pin: {unpinned}")

        def fn(action: str, args: dict, ctx: CallContext) -> dict:
            if self.transport is None:
                self._connect()
            r = self.transport.request("tools/call", {"name": mapping[action], "arguments": args})
            if r.get("isError"):
                raise PermanentError(str(r.get("content")))
            return r.get("structuredContent") or {}
        return ToolImpl(fn)
