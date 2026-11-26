"""Tool gateway (REQ 018, 021, 022, 023, 038, 053).

Every tool call passes: kill-switch -> contract/schema -> runtime authorization
(identity grant + tenant + resource + policy tier) -> bound approval for tier 3 ->
idempotency/payload binding -> bulkhead + circuit breaker + classified retry -> output
schema -> evidence. Model output or tool metadata never substitutes for any of these.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from typing import Any, Callable

import jsonschema
import yaml

from ..core.approvals import ApprovalService
from ..core.controls import Controls, ControlsUnavailable
from ..core.db import DB, j, unj
from ..core.evidence import Ledger
from ..core.identity import Principal
from ..core.policy import PolicyEngine
from ..core.telemetry import Telemetry
from ..core.tracing import TRACER, correlate
from ..core.util import digest_obj, new_id, now

REQUIRED_CONTRACT = ["name", "version", "owner", "actions", "resource_scope", "data_classes", "authentication",
                     "authorization", "timeout_s", "retry", "idempotency"]
REQUIRED_ACTION = ["input_schema", "output_schema", "side_effect"]
SIDE_EFFECTS = {"none", "read", "write", "consequential"}


_CALL_POOL = ThreadPoolExecutor(max_workers=64, thread_name_prefix="tool-call")


class ToolDenied(PermissionError):
    pass


class ToolContractError(ValueError):
    pass


class TransientError(Exception):
    """Retryable downstream failure (timeouts, 429, 503)."""


class PermanentError(Exception):
    """Non-retryable failure (4xx validation, business rejection)."""


class OutcomeUnknown(Exception):
    """Lost response: the downstream may or may not have committed."""


@dataclass
class CallContext:
    principal: Principal
    tenant: str
    project: str
    run_id: str = "adhoc"
    env: str = "sandbox"
    approval_ids: list[str] = field(default_factory=list)
    purpose: str = "delivery"
    on_call: Callable[[str], None] | None = None  # budget hook (REQ 009)
    op_id: str | None = None


@dataclass
class ToolImpl:
    fn: Callable[[str, dict, CallContext], dict]          # (action, args, ctx) -> result
    reconcile: Callable[[str], dict | None] | None = None  # op_id -> committed result or None


def validate_contract(c: dict) -> None:
    missing = [f for f in REQUIRED_CONTRACT if f not in c]
    for a, spec in (c.get("actions") or {}).items():
        missing += [f"actions.{a}.{f}" for f in REQUIRED_ACTION if f not in spec]
        if spec.get("side_effect") not in SIDE_EFFECTS:
            missing.append(f"actions.{a}.side_effect (must declare one of {sorted(SIDE_EFFECTS)})")
        if spec.get("side_effect") in ("write", "consequential") and "reversible" not in spec:
            missing.append(f"actions.{a}.reversible")
        for k in ("input_schema", "output_schema"):
            if k in spec:
                jsonschema.Draft202012Validator.check_schema(spec[k])
    if missing:
        raise ToolContractError(f"tool {c.get('name')} contract incomplete: {missing}")


class CircuitBreaker:
    def __init__(self, threshold: int = 3, cooldown_s: float = 10.0):
        self.threshold, self.cooldown = threshold, cooldown_s
        self.failures, self.opened_at, self.state = 0, 0.0, "closed"
        self.lock = threading.Lock()

    def allow(self) -> bool:
        with self.lock:
            if self.state == "open" and now() - self.opened_at >= self.cooldown:
                self.state = "half_open"
            return self.state != "open"

    def success(self):
        with self.lock:
            self.failures, self.state = 0, "closed"

    def failure(self):
        with self.lock:
            self.failures += 1
            if self.failures >= self.threshold or self.state == "half_open":
                self.state, self.opened_at = "open", now()


class ToolGateway:
    def __init__(self, db: DB, ledger: Ledger, policy: PolicyEngine, controls: Controls,
                 approvals: ApprovalService, telemetry: Telemetry):
        self.db, self.ledger, self.policy, self.controls = db, ledger, policy, controls
        self.approvals, self.tm = approvals, telemetry
        self.contracts: dict[str, dict] = {}
        self.impls: dict[str, ToolImpl] = {}
        self.breakers: dict[str, CircuitBreaker] = {}
        self.bulkheads: dict[str, threading.BoundedSemaphore] = {}
        self._op_lock = threading.Lock()
        self.inflight_grace_s = 30.0

    # ---------------- registration ----------------
    def register(self, contract: dict, impl: ToolImpl, actor: str = "platform-admin") -> str:
        validate_contract(contract)
        d = digest_obj(contract)
        self.db.execute("INSERT OR REPLACE INTO tool_contracts(name,version,digest,contract,status) VALUES(?,?,?,?,?)",
                        (contract["name"], str(contract["version"]), d, j(contract), "active"))
        self.contracts[contract["name"]] = contract
        self.impls[contract["name"]] = impl
        self.breakers[contract["name"]] = CircuitBreaker(contract.get("circuit", {}).get("threshold", 3),
                                                          contract.get("circuit", {}).get("cooldown_s", 10))
        self.bulkheads[contract["name"]] = threading.BoundedSemaphore(contract.get("max_concurrency", 4))
        self.ledger.record("tool.registered", f"{contract['name']}@{contract['version']}", actor, {"digest": d})
        return d

    def register_yaml(self, path, impl: ToolImpl) -> str:
        return self.register(yaml.safe_load(open(path, encoding="utf-8").read()), impl)

    def contract_digest(self, name: str) -> str:
        return digest_obj(self.contracts[name])

    # ---------------- invocation ----------------
    def invoke(self, ctx: CallContext, tool: str, action: str, args: dict, *, resource: str | None = None,
               op_id: str | None = None) -> dict:
        t0 = time.time()
        c = self.contracts.get(tool)
        aspec = (c or {}).get("actions", {}).get(action)
        resource = resource or args.get("resource") or f"{ctx.tenant}/{ctx.project}"
        res_tenant = args.get("tenant", resource.split("/")[0])
        try:
            if self.controls.blocked("cancel_run", ctx.run_id):
                raise ToolDenied(f"run {ctx.run_id} cancelled by operator")
            ctl = self.controls.blocked("revoke_tool", tool) or self.controls.blocked("disable_capability",
                                                                                         f"{tool}:{action}")
        except ControlsUnavailable as e:
            self._deny(ctx, tool, action, str(e))
        if ctl:
            self._deny(ctx, tool, action, f"revoked by operations control {ctl['id']}: {ctl['reason']}")
        if not c or not aspec:
            facts_unreg = {"tool_registered": False}
            d = self.policy.evaluate({"actor": {"kind": ctx.principal.kind, "id": ctx.principal.id},
                                      "action": f"{tool}.{action}", **facts_unreg})
            self._deny(ctx, tool, action, "; ".join(d.reasons) or "unregistered tool")
        try:
            jsonschema.validate(args, aspec["input_schema"])
        except jsonschema.ValidationError as e:
            self._deny(ctx, tool, action, f"input schema violation: {e.message}")
        side = aspec["side_effect"]
        facts = {
            "actor": {"kind": ctx.principal.kind, "id": ctx.principal.id, "roles": sorted(ctx.principal.roles)},
            "action": f"{tool}.{action}", "tool": tool, "side_effect": side, "env": ctx.env,
            "data_class": aspec.get("data_class", c["data_classes"][0]), "reversible": aspec.get("reversible", True),
            "resource": resource, "branch": args.get("branch"),
            "grant": ctx.principal.can(tool, action, resource),
            "tenant_match": ctx.principal.in_tenant(res_tenant) and res_tenant == ctx.tenant,
            "tool_registered": True,
        }
        tier = self.policy.classify(facts)
        if tier >= 3:
            binding = ApprovalService.binding(f"tool:{tool}.{action}", digest_obj(args), ctx.env, resource)
            # roles come from policy obligations (default release_owner; domains can name their own, e.g. advisor)
            roles = self.policy.evaluate({**facts, "approval_valid": True}).obligations.get("approval_roles") or ["release_owner"]
            try:
                self.approvals.require(ctx.approval_ids, binding, roles)
                facts["approval_valid"] = True
            except PermissionError as e:
                facts["approval_valid"] = False
                facts["approval_problem"] = str(e)
        dec = self.policy.evaluate(facts)
        if not dec.allow:
            self._deny(ctx, tool, action, "; ".join(dec.reasons + ([facts.get("approval_problem")] if facts.get("approval_problem") else [])))
        if ctx.on_call:
            ctx.on_call(tool)  # may raise BudgetExceeded
        # ---- idempotency (REQ 022) ----
        needs_op = side in ("write", "consequential") or c["idempotency"].get("required")
        if needs_op and not op_id:
            self._deny(ctx, tool, action, "consequential/write call requires a stable operation id")
        payload_hash = digest_obj({"tool": tool, "action": action, "args": args})
        if needs_op:
            prior = self._claim(op_id, tool, ctx.principal.id, payload_hash)
            if prior is not None:
                return prior
        with correlate(run=ctx.run_id, actor=ctx.principal.id, project=ctx.project), \
                TRACER.span("tool.call", tool=tool, action=action, tier=tier, run=ctx.run_id):
            result = self._execute(ctx, c, tool, action, args, op_id if needs_op else None)
        try:
            jsonschema.validate(result, aspec["output_schema"])
        except jsonschema.ValidationError as e:
            raise PermanentError(f"tool {tool}.{action} output violated contract: {e.message}")
        self.ledger.record("tool.call", f"{tool}.{action}", ctx.principal.id,
                           {"run": ctx.run_id, "tier": tier, "op_id": op_id, "resource": resource,
                            "args_digest": digest_obj(args), "result_digest": digest_obj(result),
                            "approvals": ctx.approval_ids, "policy": dec.policy_digest}, ctx.project)
        self.tm.metric("product" if ctx.purpose == "product" else "delivery", "tool_calls", 1, tool=tool,
                       outcome="ok", latency_ms=round((time.time() - t0) * 1000, 1))
        return result

    def _deny(self, ctx: CallContext, tool: str, action: str, reason: str):
        self.ledger.record("tool.denied", f"{tool}.{action}", ctx.principal.id,
                           {"run": ctx.run_id, "reason": reason, "tenant": ctx.tenant}, ctx.project)
        self.tm.metric("product" if ctx.purpose == "product" else "delivery", "tool_denied", 1, tool=tool,
                       category="policy_denied")
        raise ToolDenied(reason)

    def _claim(self, op_id: str, tool: str, caller: str, payload_hash: str) -> dict | None:
        """Returns a prior committed result (dedupe) or None if this caller now owns execution."""
        with self._op_lock, self.db.tx():
            r = self.db.one("SELECT * FROM operations WHERE op_id=?", (op_id,))
            if r is None:
                self.db.execute("INSERT INTO operations(op_id,tool,caller,payload_hash,state,created,updated)"
                                " VALUES(?,?,?,?,?,?,?)", (op_id, tool, caller, payload_hash, "pending", now(), now()))
                return None
        if r["payload_hash"] != payload_hash:
            raise ToolDenied(f"operation {op_id} reused with a different payload (payload binding violated)")
        if r["state"] == "committed":
            return {**unj(r["result"]), "_deduplicated": True}
        # pending/unknown: someone started this before (crash, lost response or concurrent duplicate)
        impl = self.impls[tool]
        if impl.reconcile:
            committed = impl.reconcile(op_id)
            if committed is not None:
                self._finish(op_id, "committed", committed)
                self.ledger.record("tool.reconciled", op_id, "tool-gateway", {"outcome": "found_committed"})
                return {**committed, "_reconciled": True}
            if r["state"] == "pending" and now() - r["updated"] < self.inflight_grace_s:
                raise ToolDenied(f"operation {op_id} in flight; duplicate suppressed")
            self.ledger.record("tool.reconciled", op_id, "tool-gateway", {"outcome": "not_committed_safe_to_retry"})
            return None
        raise ToolDenied(f"operation {op_id} outcome unknown and tool has no reconciliation; escalated")

    def _finish(self, op_id: str, state: str, result: dict | None):
        self.db.execute("UPDATE operations SET state=?, result=?, updated=? WHERE op_id=?",
                        (state, j(result) if result is not None else None, now(), op_id))

    def _execute(self, ctx: CallContext, c: dict, tool: str, action: str, args: dict, op_id: str | None) -> dict:
        br, bh = self.breakers[tool], self.bulkheads[tool]
        retry = c["retry"]
        attempts = 1 + (int(retry.get("max", 0)) if (op_id or c["actions"][action]["side_effect"] in ("none", "read")) else 0)
        last: Exception | None = None
        for i in range(attempts):
            if not br.allow():
                last = TransientError(f"circuit open for {tool}")
                self.tm.metric("delivery", "circuit_open", 1, tool=tool, category="tool_unavailable")
                break
            if not bh.acquire(timeout=c.get("queue_timeout_s", 2)):
                last = TransientError(f"bulkhead full for {tool}")
                self.tm.metric("delivery", "bulkhead_reject", 1, tool=tool, category="queue_backlog")
                break
            try:
                ctx.op_id = op_id
                fut = _CALL_POOL.submit(self.impls[tool].fn, action, args, ctx)
                try:
                    res = fut.result(timeout=float(c["timeout_s"]))
                except FuturesTimeout:
                    self.tm.metric("delivery", "tool_timeout", 1, tool=tool, category="tool_unavailable")
                    if c["actions"][action]["side_effect"] in ("write", "consequential"):
                        raise OutcomeUnknown(f"{tool}.{action} timed out after {c['timeout_s']}s; outcome must be reconciled")
                    raise TransientError(f"{tool}.{action} timed out after {c['timeout_s']}s")
                br.success()
                if op_id:
                    self._finish(op_id, "committed", res)
                return res
            except TransientError as e:
                br.failure(); last = e
                time.sleep(min(0.05 * (2 ** i), 0.5))
            except OutcomeUnknown as e:
                if op_id:
                    self._finish(op_id, "unknown", None)
                raise
            except PermanentError:
                if op_id:
                    self._finish(op_id, "failed", None)
                raise
            finally:
                bh.release()
        # exhausted / degraded: send to failed-work queue (REQ 023)
        if op_id:
            self._finish(op_id, "failed", None)
        dlq = new_id("DLQ")
        self.db.execute("INSERT INTO dlq(id,tool,caller,tenant,payload,error,approval_id,created) VALUES(?,?,?,?,?,?,?,?)",
                        (dlq, tool, ctx.principal.id, ctx.tenant, j({"action": action, "args": args, "op_id": op_id,
                                                                     "run": ctx.run_id, "project": ctx.project}),
                         str(last), j(ctx.approval_ids), now()))
        self.ledger.record("tool.dead_lettered", dlq, ctx.principal.id, {"tool": tool, "error": str(last)}, ctx.project)
        raise TransientError(f"{tool}.{action} unavailable; queued as {dlq}: {last}")

    def replay(self, dlq_id: str, operator: Principal, ctx: CallContext) -> dict:
        """Replay requires an operator and re-runs full authorization; stale approvals fail again."""
        if not operator.has_role("operator"):
            raise ToolDenied("DLQ replay requires an operator")
        r = self.db.one("SELECT * FROM dlq WHERE id=? AND replayed=0", (dlq_id,))
        if not r:
            raise KeyError(dlq_id)
        p = unj(r["payload"])
        if p.get("op_id"):
            self.db.execute("DELETE FROM operations WHERE op_id=? AND state='failed'", (p["op_id"],))
        self.ledger.record("tool.replay", dlq_id, operator.id, {"tool": r["tool"]}, ctx.project)
        res = self.invoke(ctx, r["tool"], p["action"], p["args"], op_id=p.get("op_id"))
        self.db.execute("UPDATE dlq SET replayed=1 WHERE id=?", (dlq_id,))
        return res
