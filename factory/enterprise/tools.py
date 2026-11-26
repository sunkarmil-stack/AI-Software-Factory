"""Business tools reached through the enterprise AI gateway (production profile).

A tool contract opts in with:
  endpoint:
    via: ai_gateway
    api: deposits
    actions: {create: {method: POST, path: /holds}}
    reconcile: {method: GET, path: /holds, query: operation_id}
The factory operation id is sent as the Idempotency-Key, and reconciliation asks the system of
record (through the gateway) whether that operation already committed.
"""
from __future__ import annotations

from ..tools.gateway import CallContext, OutcomeUnknown, PermanentError, ToolImpl, TransientError
from .ai_gateway import (AIGatewayClient, EnterpriseRevoked, GatewayError, GatewayThrottled, GatewayUnavailable)


def _route(template: str, method: str, args: dict) -> tuple[str, dict | None]:
    """Fill {name} path parameters from args; GET sends the remaining args as a query string."""
    import urllib.parse
    used = {k for k in args if "{" + k + "}" in template}
    path = template
    for k in used:
        path = path.replace("{" + k + "}", urllib.parse.quote(str(args[k]), safe=""))
    rest = {k: v for k, v in args.items() if k not in used}
    if method == "GET":
        q = urllib.parse.urlencode({k: ",".join(map(str, v)) if isinstance(v, list) else v for k, v in rest.items()})
        return (path + ("?" + q if q else "")), None
    return path, rest


def gateway_tool(client: AIGatewayClient, contract: dict, ledger=None) -> ToolImpl:
    ep = contract["endpoint"]

    def fn(action: str, args: dict, ctx: CallContext) -> dict:
        spec = ep["actions"].get(action)
        if not spec:
            raise PermanentError(f"{contract['name']}.{action} has no gateway route")
        path, body = _route(spec["path"], spec["method"], args)
        try:
            return client.call_api(ep["api"], spec["method"], path, body, idempotency_key=ctx.op_id)
        except EnterpriseRevoked as e:
            if ledger:
                ledger.record("tool.enterprise_revoked", f"{contract['name']}.{action}", "tool-gateway",
                              {"run": ctx.run_id, "reason": str(e)}, ctx.project)
            raise PermanentError(f"revoked by the enterprise AI gateway: {e}")
        except GatewayThrottled as e:
            raise TransientError(f"enterprise AI gateway throttled: {e}")
        except GatewayUnavailable as e:
            if e.status == 0:
                raise OutcomeUnknown(f"gateway unreachable after send: {e}") if spec["method"] != "GET" else TransientError(str(e))
            raise TransientError(str(e))
        except GatewayError as e:
            raise PermanentError(str(e))

    def reconcile(op_id: str) -> dict | None:
        rc = ep.get("reconcile")
        if not rc:
            return None
        try:
            return client.call_api(ep["api"], rc["method"], f"{rc['path']}?{rc['query']}={op_id}")
        except GatewayError as e:
            if e.status == 404:
                return None
            raise

    return ToolImpl(fn, reconcile=reconcile if ep.get("reconcile") else None)
