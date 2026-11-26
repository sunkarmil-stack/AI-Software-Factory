"""Model gateway (REQ 029-033).

Approved routing with workload-scoped identity, approved uses, data-class handling,
quotas, per-project/per-run budgets (shared across child runs via root_run), usage
accounting, circuit breaking, and explicitly-approved fallbacks only.
"""
from __future__ import annotations

import fnmatch
import json
import os
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

import yaml

from ..core.controls import Controls, ControlsUnavailable
from ..core.db import DB
from ..core.evidence import Ledger
from ..core.identity import Principal
from ..core.telemetry import Telemetry
from ..core.tracing import TRACER, correlate
from ..core.util import digest_obj, new_id, now
from ..tools.gateway import CircuitBreaker


class ModelDenied(PermissionError):
    pass


class ProviderThrottled(Exception):
    pass


class ProviderUnavailable(Exception):
    pass


class BudgetExceeded(Exception):
    pass


@dataclass
class ModelContext:
    principal: Principal
    project: str
    tenant: str
    run_id: str
    root_run: str
    purpose: str                       # e.g. delivery.implementation, product.rag
    data_class: str = "internal"
    on_usage: Callable[[int, float], None] | None = None


@dataclass
class Completion:
    output: Any
    deployment: str
    revision: str
    tokens_in: int
    tokens_out: int
    cost: float
    latency_ms: float
    fallback_used: bool = False
    limitations: list[str] = field(default_factory=list)


# ---------------- providers ----------------
OFFLINE_HANDLERS: dict[str, Callable[[dict], dict]] = {}


def offline_handler(kind: str):
    def deco(fn):
        OFFLINE_HANDLERS[kind] = fn
        return fn
    return deco


class OfflineProvider:
    """Deterministic rules provider: lets the full governed pipeline run with no model access.
    Each agent role registers a handler that produces schema-conformant output from the task."""
    revision_immutable = True

    def complete(self, dep: dict, system: str, task: dict) -> str:
        h = OFFLINE_HANDLERS.get(task.get("kind"))
        if not h:
            raise ProviderUnavailable(f"offline provider has no handler for {task.get('kind')}")
        return json.dumps(h(task))


class _HTTPProvider:
    revision_immutable = False

    def __init__(self, allowlist: list[str]):
        self.allowlist = allowlist

    def _post(self, url: str, headers: dict, body: dict, timeout: float) -> dict:
        host = urlparse(url).hostname or ""
        if not any(fnmatch.fnmatch(host, a) for a in self.allowlist):
            raise ModelDenied(f"egress to {host} not in model gateway allowlist")
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 529):
                raise ProviderThrottled(str(e))
            raise ProviderUnavailable(str(e))
        except OSError as e:
            raise ProviderUnavailable(str(e))


class AnthropicProvider(_HTTPProvider):
    def complete(self, dep: dict, system: str, task: dict) -> str:
        key = os.environ.get(dep.get("secret_ref", "ANTHROPIC_API_KEY"))
        if not key:
            raise ProviderUnavailable("no credential bound to model deployment")
        body = {"model": dep["model"], "max_tokens": dep.get("max_tokens", 4096), "system": system,
                "temperature": dep.get("temperature", 0),
                "messages": [{"role": "user", "content": json.dumps(task)}]}
        r = self._post(dep.get("endpoint", "https://api.anthropic.com/v1/messages"),
                       {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                       body, dep.get("timeout_s", 120))
        return "".join(b.get("text", "") for b in r.get("content", []))


class AzureOpenAIProvider(_HTTPProvider):
    def complete(self, dep: dict, system: str, task: dict) -> str:
        key = os.environ.get(dep.get("secret_ref", "AZURE_OPENAI_API_KEY"))
        if not key:
            raise ProviderUnavailable("no credential bound to model deployment")
        url = f"{dep['endpoint']}/openai/deployments/{dep['model']}/chat/completions?api-version={dep.get('api_version', '2024-10-21')}"
        r = self._post(url, {"api-key": key, "content-type": "application/json"},
                       {"messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(task)}],
                        "temperature": dep.get("temperature", 0), "response_format": {"type": "json_object"}},
                       dep.get("timeout_s", 120))
        return r["choices"][0]["message"]["content"]


class EnterpriseRevokedModel(ProviderUnavailable):
    """The enterprise AI gateway disabled this model; an approved fallback may still serve the call."""


class ContentRejected(ModelDenied):
    pass


class AIGatewayProvider:
    """Production provider: every model call goes through the enterprise AI gateway (no direct egress)."""
    revision_immutable = False

    def __init__(self, client):
        self.client = client

    def complete(self, dep: dict, system: str, task: dict) -> str:
        from ..enterprise.ai_gateway import ContentBlocked, EnterpriseRevoked, GatewayThrottled, GatewayUnavailable, GatewayError
        try:
            text, _usage = self.client.chat(dep["model"], system, json.dumps(task), temperature=dep.get("temperature", 0),
                                            max_tokens=dep.get("max_tokens", 4096))
            return text
        except GatewayThrottled as e:
            raise ProviderThrottled(f"enterprise AI gateway throttled (retry after {e.retry_after}s)")
        except EnterpriseRevoked as e:
            raise EnterpriseRevokedModel(str(e))
        except ContentBlocked as e:
            raise ContentRejected(f"enterprise content safety blocked the request: {e}")
        except GatewayUnavailable as e:
            raise ProviderUnavailable(str(e))
        except GatewayError as e:
            raise ProviderUnavailable(str(e))


class FaultInjector:
    """Wraps a provider to simulate throttling/outage (ACC 08)."""

    def __init__(self, inner, throttle_every: int = 0, down: bool = False):
        self.inner, self.throttle_every, self.down, self.n = inner, throttle_every, down, 0
        self.revision_immutable = inner.revision_immutable
        self.lock = threading.Lock()

    def complete(self, dep, system, task):
        with self.lock:
            self.n += 1
            n = self.n
        if self.down:
            raise ProviderUnavailable("injected outage")
        if self.throttle_every and n % self.throttle_every != 0:
            raise ProviderThrottled("injected 429")
        return self.inner.complete(dep, system, task)


# ---------------- gateway ----------------
class ModelGateway:
    def __init__(self, db: DB, ledger: Ledger, controls: Controls, telemetry: Telemetry, config: dict):
        self.db, self.ledger, self.controls, self.tm = db, ledger, controls, telemetry
        self.cfg = config
        self.deployments = {d["id"]: d for d in config["deployments"]}
        allow = config.get("egress_allowlist", [])
        self.providers: dict[str, Any] = {"offline": OfflineProvider(), "anthropic": AnthropicProvider(allow),
                                          "azure_openai": AzureOpenAIProvider(allow)}
        self.breakers: dict[str, CircuitBreaker] = {k: CircuitBreaker(3, 15) for k in self.deployments}
        self._rate: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @classmethod
    def load(cls, db, ledger, controls, telemetry, path) -> "ModelGateway":
        return cls(db, ledger, controls, telemetry, yaml.safe_load(open(path).read()))

    def _authorize(self, ctx: ModelContext, dep_id: str) -> dict:
        dep = self.deployments.get(dep_id)
        if not dep:
            raise ModelDenied(f"deployment {dep_id} is not approved")
        try:
            if self.controls.blocked("narrow_model", dep_id):
                raise ModelDenied(f"deployment {dep_id} narrowed by operations control")
        except ControlsUnavailable as e:
            raise ModelDenied(str(e))
        if not ctx.principal.can("model", "invoke", dep_id):
            raise ModelDenied(f"{ctx.principal.id} has no workload grant for {dep_id}")
        if not any(fnmatch.fnmatch(ctx.purpose, u) for u in dep["approved_uses"]):
            raise ModelDenied(f"{dep_id} not approved for use {ctx.purpose}")
        if ctx.data_class not in dep["data_classes"]:
            raise ModelDenied(f"{dep_id} not approved for data class {ctx.data_class}")
        return dep

    def _quota(self, ctx: ModelContext, dep: dict) -> None:
        rpm = dep.get("rpm_per_project", 600)
        with self._lock:
            w = [t for t in self._rate.get(ctx.project, []) if now() - t < 60]
            if len(w) >= rpm:
                self.tm.metric("factory", "model_quota_reject", 1, category="provider_throttled")
                raise ProviderThrottled(f"project {ctx.project} model quota {rpm}/min reached")
            w.append(now())
            self._rate[ctx.project] = w

    def _budget(self, ctx: ModelContext, est_cost: float) -> None:
        proj = self.db.one("SELECT budget_usd FROM projects WHERE id=?", (ctx.project,))
        if proj and proj["budget_usd"] is not None:
            spent = self.db.one("SELECT COALESCE(SUM(cost),0) s FROM usage WHERE project=?", (ctx.project,))["s"]
            if spent + est_cost > proj["budget_usd"]:
                self.tm.metric("factory", "budget_breach", 1, category="budget_exceeded")
                raise BudgetExceeded(f"project {ctx.project} budget {proj['budget_usd']} exhausted")

    def complete(self, ctx: ModelContext, deployment: str, system: str, task: dict,
                 prompt_ref: dict | None = None) -> Completion:
        dep = self._authorize(ctx, deployment)
        chain = [dep] + [self.deployments[f] for f in dep.get("approved_fallbacks", []) if f in self.deployments]
        last: Exception | None = None
        for i, d in enumerate(chain):
            if i > 0:
                # fallback must itself be approved for the same use and data class (REQ 033)
                try:
                    self._authorize(ctx, d["id"])
                except ModelDenied as e:
                    last = e
                    self.ledger.record("model.fallback_refused", d["id"], "model-gateway", {"reason": str(e)}, ctx.project)
                    continue
                if deployment not in d.get("fallback_for", []):
                    continue
            if not self.breakers[d["id"]].allow():
                last = ProviderUnavailable(f"circuit open for {d['id']}")
                continue
            est_in = len(system + json.dumps(task)) // 4
            est_cost = est_in / 1000 * d.get("cost_per_1k_in", 0)
            self._budget(ctx, est_cost)
            self._quota(ctx, d)
            t0 = time.time()
            try:
                if d["provider"] not in self.providers:
                    raise ProviderUnavailable(f"provider {d['provider']} not configured in this profile")
                with correlate(run=ctx.run_id, actor=ctx.principal.id, project=ctx.project), \
                        TRACER.span("model.call", deployment=d["id"], purpose=ctx.purpose, run=ctx.run_id):
                    text = self.providers[d["provider"]].complete(d, system, task)
                self.breakers[d["id"]].success()
            except EnterpriseRevokedModel as e:
                self.ledger.record("model.enterprise_revoked", d["id"], "model-gateway", {"reason": str(e)}, ctx.project)
                self.tm.metric("factory", "provider_error", 1, deployment=d["id"], category="policy_denied")
                last = e
                continue
            except (ProviderThrottled, ProviderUnavailable) as e:
                self.breakers[d["id"]].failure()
                self.tm.metric("factory", "provider_error", 1, deployment=d["id"],
                               category="provider_throttled" if isinstance(e, ProviderThrottled) else "tool_unavailable")
                last = e
                continue
            lat = (time.time() - t0) * 1000
            t_out = len(text) // 4
            cost = est_cost + t_out / 1000 * d.get("cost_per_1k_out", 0)
            self.db.execute("INSERT INTO usage(id,root_run,run,project,tenant,kind,units,cost,latency_ms,ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (new_id("USE"), ctx.root_run, ctx.run_id, ctx.project, ctx.tenant, "model",
                             est_in + t_out, cost, lat, now()))
            if ctx.on_usage:
                ctx.on_usage(est_in + t_out, cost)
            limitations = [] if self.providers[d["provider"]].revision_immutable else [
                f"provider {d['provider']} does not guarantee immutable inference revision for {d['model']}"]
            self.ledger.record("model.call", d["id"], ctx.principal.id,
                               {"run": ctx.run_id, "purpose": ctx.purpose, "revision": d.get("revision", d["model"]),
                                "prompt": prompt_ref, "settings": {"temperature": d.get("temperature", 0)},
                                "task_digest": digest_obj(task), "tokens": est_in + t_out, "fallback": i > 0,
                                "limitations": limitations}, ctx.project)
            self.tm.metric("product" if ctx.purpose.startswith("product") else "delivery", "model_calls", 1,
                           deployment=d["id"])
            try:
                out = json.loads(text)
            except json.JSONDecodeError:
                out = text
            return Completion(out, d["id"], d.get("revision", d["model"]), est_in, t_out, cost, lat, i > 0, limitations)
        raise ProviderUnavailable(f"no approved deployment available for {deployment}: {last}")
