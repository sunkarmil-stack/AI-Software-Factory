"""Deployment targets for Pipeline D (pluggable; selected by settings.deployment_target).

local          : dev/reference. Writes desired state next to an extracted artifact and smoke-tests it.
gitops         : production. Commits the approved artifact digest and rollout stage to a protected
                 environment repository (Argo CD / Flux reconcile it into AKS, e.g. with Argo Rollouts
                 canary weights). Only the release service can push. Environments never rebuild.
harness-helios : production adapter for the enterprise Harness (validation) -> Helios (deployment)
                 path. Interface defined; implementation pending confirmation of their APIs.
"""
from __future__ import annotations

import gzip
import io
import json
import shutil
import tarfile
from pathlib import Path

import yaml

from .repo import git


class NotConfigured(RuntimeError):
    pass


class DeploymentTarget:
    name = "abstract"

    def apply(self, project: str, env: str, state: dict) -> str:   # returns a reference (path, commit, id)
        raise NotImplementedError

    def verify(self, project: str, env: str, digest: str, spec: dict) -> list[str]:
        raise NotImplementedError

    def desired(self, project: str, env: str) -> dict | None:
        raise NotImplementedError

    def runtime_drift(self, project: str, env: str, digest: str) -> list[str]:
        return []


def extract_artifact(registry: Path, digest: str, dest: Path) -> Path:
    if not dest.exists():
        blob = (registry / f"{digest.split(':')[1]}.tar.gz").read_bytes()
        dest.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(blob))) as tf:
            tf.extractall(dest, filter="data")
    return dest


class LocalTarget(DeploymentTarget):
    name = "local"

    def __init__(self, release):
        self.r = release

    def apply(self, project, env, state):
        d = self.r._deploy_dir(project, env)
        d.mkdir(parents=True, exist_ok=True)
        extract_artifact(self.r.f.ci.registry, state["digest"], d / state["digest"].split(":")[1][:16])
        (d / "desired.json").write_text(json.dumps(state))
        return str(d / "desired.json")

    def verify(self, project, env, digest, spec):
        return self.r.smoke(self.r._deploy_dir(project, env) / digest.split(":")[1][:16], spec)

    def desired(self, project, env):
        p = self.r._deploy_dir(project, env) / "desired.json"
        return json.loads(p.read_text()) if p.exists() else None

    def runtime_drift(self, project, env, digest):
        from ..core.util import sha256_bytes
        from .ci import BUILD_INPUTS, deterministic_tar
        path = self.r._deploy_dir(project, env) / digest.split(":")[1][:16]
        if sha256_bytes(deterministic_tar(path, BUILD_INPUTS)) != digest:
            return ["runtime files differ from approved artifact (out-of-band change)"]
        return []


class GitOpsTarget(DeploymentTarget):
    """Environment repo layout: envs/<env>/<project>/release.yaml + kustomization.yaml."""
    name = "gitops"

    def __init__(self, release, repo_url: str = ""):
        self.r = release
        repo = release.f.repo
        self.bare = Path(repo_url) if repo_url and not repo_url.startswith(("http", "git@")) else repo.root / "environments.git"
        if not self.bare.exists():
            repo.create_env_repo(self.bare)
        self.work = release.f.home / "gitops-work"

    def _checkout(self) -> Path:
        if self.work.exists():
            git(["fetch", "-q", "origin"], self.work)
            git(["reset", "-q", "--hard", "origin/main"], self.work)
        else:
            git(["clone", "-q", str(self.bare), str(self.work)], self.work.parent)
        return self.work

    def apply(self, project, env, state):
        w = self._checkout()
        d = w / "envs" / env / project
        d.mkdir(parents=True, exist_ok=True)
        (d / "release.yaml").write_text(yaml.safe_dump({
            "release": state["release"], "image": f"registry.internal/{project}@{state['digest']}",
            "digest": state["digest"], "stage": state["stage"], "canaryWeight": state["weight"],
            "previous": state.get("previous")}, sort_keys=True))
        (d / "kustomization.yaml").write_text(yaml.safe_dump({
            "apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization",
            "resources": [f"../../../base/{project}"],
            "images": [{"name": project, "newName": f"registry.internal/{project}", "digest": state["digest"]}]},
            sort_keys=False))
        git(["add", "-A"], w)
        env_ = self.r.f.repo._svc_env("release-service")
        if git(["status", "--porcelain"], w):
            git(["commit", "-q", "-m", f"{env}/{project}: {state['release']} {state['stage']} {state['weight']}%"], w, env_)
            git(["push", "-q", "origin", "HEAD:main"], w, env_)
        return git(["rev-parse", "HEAD"], w)

    def verify(self, project, env, digest, spec):
        # In production this reads rollout health (Argo Rollouts analysis / Azure Monitor SLOs).
        # Here the approved artifact is exercised with the spec's read-only acceptance checks.
        scratch = self.r.f.home / "verify" / env / project / digest.split(":")[1][:16]
        extract_artifact(self.r.f.ci.registry, digest, scratch)
        return self.r.smoke(scratch, spec)

    def desired(self, project, env):
        out = git(["show", f"main:envs/{env}/{project}/release.yaml"], self.bare, check=False)
        return yaml.safe_load(out) if out else None


class HarnessHeliosTarget(DeploymentTarget):
    """Enterprise path: Harness runs validation stages, Helios performs the deployment.

    Contract this adapter must implement once their APIs are confirmed (Phase 0):
      apply  -> trigger the Helios deployment for (project, env) with the exact artifact digest, the signed
                release manifest digest, the bound approval ids and the change record; return the execution id
      verify -> read Helios rollout health for that execution; return failures
      desired-> read the environment's desired release from Helios
    Harness results arrive as signed attestations and are verified like CI provenance before promotion.
    """
    name = "harness-helios"

    def __init__(self, release, **_):
        self.r = release

    def apply(self, project, env, state):
        raise NotConfigured("Harness/Helios adapter not configured: confirm their deployment and status APIs (§15)")

    def verify(self, project, env, digest, spec):
        raise NotConfigured("Harness/Helios adapter not configured")

    def desired(self, project, env):
        raise NotConfigured("Harness/Helios adapter not configured")


def make_target(release, kind: str, repo_url: str = "") -> DeploymentTarget:
    return {"local": lambda: LocalTarget(release), "gitops": lambda: GitOpsTarget(release, repo_url),
            "harness-helios": lambda: HarnessHeliosTarget(release)}[kind]()
