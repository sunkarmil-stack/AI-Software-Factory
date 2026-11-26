"""Runtime profiles: `dev` (local reference implementation) and `production`.

The production profile is fail-closed: `preflight()` lists every production invariant that
is not met, and the Factory refuses to start in production while any failure remains,
unless the check has a recorded, owned, unexpired exemption (the same exception model the
requirements use for policy: owner, rationale, scope, expiry).

Load order: $FACTORY_CONFIG (YAML file) -> $FACTORY_PROFILE -> defaults (dev).
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .core.util import now


@dataclass
class AIGatewaySettings:
    base_url: str = ""                      # e.g. https://aigw.internal.bank/ (APIM AI gateway)
    audience: str = ""                      # Entra app ID URI of the gateway, token scope = audience/.default
    models_path: str = "models/chat/completions"
    mcp_path: str = "mcp/{server}"
    api_path: str = "apis/{api}{path}"
    timeout_s: float = 60.0


@dataclass
class EntraSettings:
    tenant_id: str = ""
    authority_host: str = "https://login.microsoftonline.com"
    issuer: str = ""                        # https://login.microsoftonline.com/<tenant>/v2.0
    audience: str = ""                      # factory API app ID URI
    jwks_url: str = ""
    principal_claim: str = "preferred_username"
    role_map: dict = field(default_factory=dict)      # Entra app role -> factory role
    tenant_map: dict = field(default_factory=dict)    # Entra group/claim value -> factory tenant


@dataclass
class WorkloadSettings:
    client_id: str = ""                     # AZURE_CLIENT_ID (workload identity)
    tenant_id: str = ""
    token_file: str = ""                    # AZURE_FEDERATED_TOKEN_FILE
    authority_host: str = "https://login.microsoftonline.com"


@dataclass
class KeyVaultSettings:
    url: str = ""                           # https://<vault>.vault.azure.net
    audience: str = "https://vault.azure.net"
    api_version: str = "7.4"
    keys: dict = field(default_factory=lambda: {"ci-builder": "factory-ci-builder", "release-service": "factory-release",
                                                "skill-registry": "factory-skill-registry",
                                                "approval-service": "factory-approval", "policy-service": "factory-policy"})


@dataclass
class Settings:
    profile: str = "dev"
    database_url: str = ""                  # sqlite:///path or postgresql://...
    auth: str = "dev-tokens"                # dev-tokens | entra
    signer: str = "local"                   # local | keyvault
    secrets: str = "local"                  # local | keyvault
    models_catalog: str = "models.yaml"
    sandbox_backend: str = "process"        # process | container | kubernetes
    sandbox_runtime_class: str = ""         # e.g. AKS pod sandboxing runtime class (verify name for your cluster)
    sandbox_namespace: str = "factory-sandboxes"
    deployment_target: str = "local"        # local | gitops | harness-helios
    gitops_repo: str = ""
    otel_endpoint: str = ""                 # OTLP/HTTP collector base URL
    service_name: str = "ai-software-factory"
    execution: str = "inline"               # inline (API runs work) | queue (separate workers)
    ai_gateway: AIGatewaySettings = field(default_factory=AIGatewaySettings)
    entra: EntraSettings = field(default_factory=EntraSettings)
    workload: WorkloadSettings = field(default_factory=WorkloadSettings)
    keyvault: KeyVaultSettings = field(default_factory=KeyVaultSettings)
    exemptions: list = field(default_factory=list)     # [{check, owner, reason, expires}]

    @property
    def production(self) -> bool:
        return self.profile == "production"


def _merge(dc, data: dict):
    for k, v in (data or {}).items():
        if not hasattr(dc, k):
            raise ValueError(f"unknown setting {type(dc).__name__}.{k}")
        cur = getattr(dc, k)
        if hasattr(cur, "__dataclass_fields__") and isinstance(v, dict):
            _merge(cur, v)
        else:
            setattr(dc, k, v)
    return dc


def load(path: str | Path | None = None, **overrides: Any) -> Settings:
    s = Settings()
    path = path or os.environ.get("FACTORY_CONFIG")
    if path:
        _merge(s, yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {})
    elif os.environ.get("FACTORY_PROFILE"):
        s.profile = os.environ["FACTORY_PROFILE"]
    if os.environ.get("FACTORY_DATABASE_URL"):
        s.database_url = os.environ["FACTORY_DATABASE_URL"]
    w = s.workload
    w.client_id = w.client_id or os.environ.get("AZURE_CLIENT_ID", "")
    w.tenant_id = w.tenant_id or os.environ.get("AZURE_TENANT_ID", "")
    w.token_file = w.token_file or os.environ.get("AZURE_FEDERATED_TOKEN_FILE", "")
    w.authority_host = os.environ.get("AZURE_AUTHORITY_HOST", w.authority_host)
    _merge(s, overrides)
    return s


# ---------------------------------------------------------------- preflight
@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    exempted_by: str | None = None


class PreflightError(RuntimeError):
    def __init__(self, failures: list[Check]):
        super().__init__("production preflight failed: " + "; ".join(f"{c.name}: {c.detail}" for c in failures))
        self.failures = failures


def preflight(s: Settings, models_cfg: dict | None = None, tool_contracts: list[dict] | None = None) -> list[Check]:
    checks: list[Check] = []

    def add(name, ok, detail):
        checks.append(Check(name, bool(ok), detail))

    add("database", s.database_url.startswith(("postgresql://", "postgres://")),
        f"production requires PostgreSQL (got {s.database_url.split(':')[0] or 'default sqlite'})")
    add("auth", s.auth == "entra" and s.entra.issuer and s.entra.audience and s.entra.jwks_url,
        "production requires Entra ID token validation (issuer, audience, jwks_url); dev tokens are disabled")
    add("workload_identity", s.workload.client_id and s.workload.tenant_id and s.workload.token_file,
        "workload identity federation (AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_FEDERATED_TOKEN_FILE) required")
    add("signer", s.signer == "keyvault" and s.keyvault.url, "artifact/approval signing must use Key Vault keys")
    add("secrets", s.secrets == "keyvault" and s.keyvault.url, "secrets must come from Key Vault")
    add("ai_gateway", s.ai_gateway.base_url.startswith("https://") and s.ai_gateway.audience,
        "all model and business-tool traffic must go through the enterprise AI gateway over HTTPS")
    if models_cfg is not None:
        direct = [d["id"] for d in models_cfg.get("deployments", []) if d.get("provider") != "ai_gateway"]
        add("model_egress", not direct, f"deployments bypassing the enterprise AI gateway: {direct}" if direct
            else "all model deployments route via the AI gateway")
    if tool_contracts is not None:
        bypass = [c["name"] for c in tool_contracts if not c.get("internal") and
                  (c.get("endpoint") or {}).get("via") != "ai_gateway"]
        add("tool_egress", not bypass, f"business tools without an AI-gateway endpoint: {bypass}" if bypass
            else "all business tools route via the AI gateway")
    add("sandbox", s.sandbox_backend in ("container", "kubernetes") and
        (s.sandbox_backend != "kubernetes" or s.sandbox_runtime_class),
        "sandboxes must use an assessed isolation backend (kubernetes with a sandbox runtime class, or container)")
    if s.sandbox_backend == "container":
        add("sandbox_runtime", shutil.which("docker") is not None, "docker CLI not found for container sandboxes")
    add("deployment_target", s.deployment_target in ("gitops", "harness-helios"),
        "production promotion must use a GitOps or Harness/Helios deployment target")
    add("telemetry", s.otel_endpoint.startswith(("https://", "http://")), "OTLP telemetry endpoint required")
    add("execution", s.execution == "queue", "API and workers must be separated (execution: queue)")
    for c in checks:
        if c.ok:
            continue
        ex = next((e for e in s.exemptions if e.get("check") == c.name), None)
        if ex and ex.get("owner") and ex.get("reason") and float(ex.get("expires", 0)) > now():
            c.exempted_by = f"{ex['owner']}: {ex['reason']} (expires {ex['expires']})"
    return checks


def failures(checks: list[Check]) -> list[Check]:
    return [c for c in checks if not c.ok and not c.exempted_by]
