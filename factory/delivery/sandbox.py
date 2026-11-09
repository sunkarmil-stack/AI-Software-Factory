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
    def __init__(self, root: Path, ledger: Ledger, allow_dev_isolation: bool = True):
        self.root, self.ledger = Path(root), ledger
        self.backend = "container" if os.environ.get("FACTORY_SANDBOX") == "container" and docker_available() else "process"
        self.allow_dev_isolation = allow_dev_isolation
        self.active: dict[str, Sandbox] = {}

    def create(self, project: str, task: str, risk_tier: int) -> Sandbox:
        if self.backend == "process" and risk_tier >= 2 and not self.allow_dev_isolation:
            raise PermissionError("risk tier >= 2 requires an assessed isolation backend (container/gVisor)")
        sb = Sandbox(self.root, self.ledger, project, task, self.backend)
        self.active[sb.id] = sb
        return sb
