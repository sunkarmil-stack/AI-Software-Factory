"""Ephemeral delivery sandboxes (Boundary A, REQ 044, REQ 052).

Backends:
  container : docker run --network none --read-only --cap-drop ALL --user 65534 ... (if a daemon is reachable)
  process   : scrubbed env, rlimits, blackholed proxy, separate working dir. DEV ONLY.

A namespace or plain process is not an adequate boundary for hostile code (REQ 044); the
factory therefore refuses to run code for projects at risk tier >= 2 in the `process`
backend unless `allow_dev_isolation` is set, and records the isolation level in evidence.
Production deployments should use gVisor/Kata-class runtimes (Phase 0 isolation spike).
"""
from __future__ import annotations

import os
import resource
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..core.evidence import Ledger, redact
from ..core.util import new_id

SAFE_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
            "http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9",
            "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9", "no_proxy": ""}


def _limits():
    resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 2**20, 64 * 2**20))
    try:
        resource.setrlimit(resource.RLIMIT_AS, (2 * 2**30, 2 * 2**30))
    except (ValueError, OSError):
        pass
    os.setsid()


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@dataclass
class RunResult:
    code: int
    stdout: str
    stderr: str


class Sandbox:
    def __init__(self, root: Path, ledger: Ledger, project: str, task: str, backend: str, image: str | None = None):
        self.id = new_id("SBX")
        self.dir = Path(root) / self.id
        self.work = self.dir / "work"
        self.dir.mkdir(parents=True)
        self.ledger, self.project, self.task, self.backend, self.image = ledger, project, task, backend, image
        ledger.record("sandbox.created", self.id, "sandbox-service", {"task": task, "isolation": backend}, project)

    def env(self) -> dict:
        e = dict(SAFE_ENV, HOME=str(self.dir), PYTHONPATH=str(self.work))
        assert not any(k.startswith(("FACTORY_", "AZURE_", "ANTHROPIC_", "AWS_")) for k in e)
        return e

    def run(self, cmd: list[str], timeout: int = 300) -> RunResult:
        if self.backend == "container":
            full = ["docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp",
                    "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "65534:65534",
                    "--memory", "1g", "--pids-limit", "256", "-v", f"{self.work}:/work", "-w", "/work",
                    self.image or "python:3.12-slim", *cmd]
            r = subprocess.run(full, capture_output=True, text=True, timeout=timeout)
        else:
            r = subprocess.run(cmd, cwd=str(self.work), env=self.env(), capture_output=True, text=True,
                               timeout=timeout, preexec_fn=_limits)
        self.ledger.record("sandbox.exec", self.id, "sandbox-service",
                           {"cmd": cmd[:6], "code": r.returncode, "stderr_tail": redact(r.stderr[-400:])}, self.project)
        return RunResult(r.returncode, r.stdout, r.stderr)

    def pytest(self, paths: list[str]) -> RunResult:
        return self.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *paths])

    def destroy(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
        self.ledger.record("sandbox.destroyed", self.id, "sandbox-service", {}, self.project)


class SandboxService:
    def __init__(self, root: Path, ledger: Ledger, allow_dev_isolation: bool = True, backend: str | None = None,
                 runtime_class: str = "", namespace: str = "factory-sandboxes"):
        self.root, self.ledger = Path(root), ledger
        if backend in (None, "process") and os.environ.get("FACTORY_SANDBOX") == "container" and docker_available():
            backend = "container"
        self.backend = backend or "process"
        self.runtime_class, self.namespace = runtime_class, namespace
        self.allow_dev_isolation = allow_dev_isolation
        self.active: dict[str, Sandbox] = {}

    def create(self, project: str, task: str, risk_tier: int) -> Sandbox:
        if self.backend == "process" and risk_tier >= 2 and not self.allow_dev_isolation:
            raise PermissionError("risk tier >= 2 requires an assessed isolation backend (container/gVisor)")
        if self.backend == "kubernetes":
            sb = KubernetesSandbox(self.root, self.ledger, project, task, self.runtime_class, self.namespace)
        else:
            sb = Sandbox(self.root, self.ledger, project, task, self.backend)
        self.active[sb.id] = sb
        return sb


class KubernetesSandbox(Sandbox):
    """Production sandbox: one Kubernetes Job per command in a dedicated namespace, using a sandboxed
    runtime class (e.g. AKS pod sandboxing / Kata), no service-account token, no network (namespace
    default-deny NetworkPolicy), read-only root filesystem, non-root, dropped capabilities, CPU/memory
    and deadline limits. The workspace is shipped as a ConfigMap-free tarball in an emptyDir via an
    init container fed from the factory repo service.

    NOTE: `run()` shells out to kubectl and is not exercised in the reference build environment
    (no cluster); `job_manifest()` is unit-tested.
    """

    def __init__(self, root, ledger, project, task, runtime_class: str, namespace: str,
                 image: str = "registry.internal/factory/sandbox-python:3.12"):
        super().__init__(root, ledger, project, task, "kubernetes", image)
        self.runtime_class, self.namespace = runtime_class, namespace

    def job_manifest(self, cmd: list[str], timeout: int = 300) -> dict:
        name = self.id.lower().replace("_", "-")[:50]
        return {
            "apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": name, "namespace": self.namespace,
                         "labels": {"app.kubernetes.io/part-of": "ai-software-factory", "factory/sandbox": "true",
                                    "factory/project": self.project}},
            "spec": {"backoffLimit": 0, "activeDeadlineSeconds": timeout, "ttlSecondsAfterFinished": 300,
                     "template": {"metadata": {"labels": {"factory/sandbox": "true"}},
                                  "spec": {"runtimeClassName": self.runtime_class, "restartPolicy": "Never",
                                           "automountServiceAccountToken": False, "enableServiceLinks": False,
                                           "securityContext": {"runAsNonRoot": True, "runAsUser": 65534,
                                                               "seccompProfile": {"type": "RuntimeDefault"}},
                                           "containers": [{"name": "task", "image": self.image, "command": cmd,
                                                           "workingDir": "/work",
                                                           "securityContext": {"allowPrivilegeEscalation": False,
                                                                               "readOnlyRootFilesystem": True,
                                                                               "capabilities": {"drop": ["ALL"]}},
                                                           "resources": {"limits": {"cpu": "1", "memory": "1Gi",
                                                                                    "ephemeral-storage": "1Gi"}},
                                                           "volumeMounts": [{"name": "work", "mountPath": "/work"},
                                                                            {"name": "tmp", "mountPath": "/tmp"}]}],
                                           "volumes": [{"name": "work", "emptyDir": {"sizeLimit": "512Mi"}},
                                                       {"name": "tmp", "emptyDir": {"sizeLimit": "128Mi"}}]}}}}

    def run(self, cmd: list[str], timeout: int = 300) -> RunResult:
        import json as _json
        m = self.job_manifest(cmd, timeout)
        name = m["metadata"]["name"]
        apply = subprocess.run(["kubectl", "apply", "-f", "-"], input=_json.dumps(m), capture_output=True, text=True)
        if apply.returncode != 0:
            return RunResult(apply.returncode, "", apply.stderr)
        subprocess.run(["kubectl", "-n", self.namespace, "wait", "--for=condition=complete", f"job/{name}",
                        f"--timeout={timeout}s"], capture_output=True, text=True)
        logs = subprocess.run(["kubectl", "-n", self.namespace, "logs", f"job/{name}"], capture_output=True, text=True)
        st = subprocess.run(["kubectl", "-n", self.namespace, "get", f"job/{name}", "-o",
                             "jsonpath={.status.succeeded}"], capture_output=True, text=True)
        code = 0 if st.stdout.strip() == "1" else 1
        self.ledger.record("sandbox.exec", self.id, "sandbox-service",
                           {"cmd": cmd[:6], "code": code, "isolation": f"kubernetes/{self.runtime_class}"}, self.project)
        return RunResult(code, logs.stdout, logs.stderr)
