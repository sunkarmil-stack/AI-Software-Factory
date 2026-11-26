"""Repository adapter (git) with server-side branch protection (Boundary A).

Agents push only to agent/* branches. A pre-receive hook in the bare repository rejects
writes to protected branches unless the push carries the repo-service credential, which is
never present in sandboxes. Merges happen only through `merge()` after bound engineer
review + green trusted CI on the exact head commit.
"""
from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import tarfile
from pathlib import Path

from ..core.platform import IS_WINDOWS, TEXT, child_env, rmtree
from ..core.db import DB, j, unj
from ..core.evidence import Ledger
from ..core.util import digest_obj, new_id, now

HOOK = """#!/bin/sh
while read old new ref; do
  case "$ref" in
    refs/heads/main|refs/heads/master|refs/heads/release/*)
      if [ "$FACTORY_REPO_SERVICE_TOKEN" != "{token}" ]; then
        echo "REJECTED: $ref is protected; only the repo service may update it" >&2
        exit 1
      fi;;
  esac
done
exit 0
"""


GIT = shutil.which("git") or "git"   # resolved once; on Windows the minimal env may lack git's directory


def git(args: list[str], cwd: Path, env: dict | None = None, check: bool = True) -> str:
    e = child_env({"HOME": str(cwd), "GIT_AUTHOR_NAME": "factory", "GIT_AUTHOR_EMAIL": "factory@local",
                   "GIT_COMMITTER_NAME": "factory", "GIT_COMMITTER_EMAIL": "factory@local", "GIT_CONFIG_NOSYSTEM": "1",
                   **(env or {})}, path=None if IS_WINDOWS else "/usr/bin:/bin")
    r = subprocess.run([GIT, *args], cwd=str(cwd), env=e, capture_output=True, **TEXT)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


class RepoService:
    def __init__(self, db: DB, ledger: Ledger, root: Path):
        self.db, self.ledger = db, ledger
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=True)
        tok_file = self.root / ".service-token"
        if not tok_file.exists():
            tok_file.write_text(secrets.token_hex(16), encoding="utf-8"); tok_file.chmod(0o600)
        self._token = tok_file.read_text(encoding="utf-8")

    def bare(self, project: str) -> Path:
        return self.root / f"{project}.git"

    def _svc_env(self, name: str = "repo-service") -> dict:
        return {"FACTORY_REPO_SERVICE_TOKEN": self._token, "GIT_AUTHOR_NAME": name, "GIT_COMMITTER_NAME": name}

    def create(self, project: str, template: Path, metadata: dict) -> str:
        bare = self.bare(project)
        if bare.exists():
            return git(["rev-parse", "main"], bare)
        git(["init", "--bare", "-b", "main", str(bare)], self.root)
        (bare / "hooks" / "pre-receive").write_text(HOOK.replace("{token}", self._token), encoding="utf-8")
        (bare / "hooks" / "pre-receive").chmod(0o755)
        work = self.root / f".init-{project}"
        rmtree(work, ignore_errors=True)
        shutil.copytree(template, work)
        for p in work.rglob("*"):
            if p.is_file() and p.suffix in {".yaml", ".toml", ".tf"}:
                p.write_text(p.read_text(encoding="utf-8").replace("SERVICE_NAME", project), encoding="utf-8")
        (work / ".factory").mkdir(exist_ok=True)
        (work / ".factory" / "project.json").write_text(j(metadata), encoding="utf-8")
        git(["init", "-b", "main"], work)
        git(["add", "-A"], work)
        git(["commit", "-m", f"chore: onboard {project} from template"], work)
        git(["push", str(bare), "main"], work, self._svc_env())
        sha = git(["rev-parse", "HEAD"], work)
        rmtree(work)
        self.ledger.record("repo.created", project, "repo-service", {"sha": sha, "template": template.name}, project)
        return sha

    def create_env_repo(self, bare: Path) -> None:
        """Protected environment (GitOps) repository: only service identities can push to main."""
        bare = Path(bare)
        git(["init", "--bare", "-b", "main", str(bare)], bare.parent)
        (bare / "hooks" / "pre-receive").write_text(HOOK.replace("{token}", self._token), encoding="utf-8")
        (bare / "hooks" / "pre-receive").chmod(0o755)
        work = self.root / f".init-{bare.stem}"
        rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        (work / "README.md").write_text("Environment desired state. Written only by the factory release service.\n", encoding="utf-8")
        git(["init", "-b", "main"], work)
        git(["add", "-A"], work)
        git(["commit", "-q", "-m", "init environments"], work, self._svc_env("release-service"))
        git(["push", "-q", str(bare), "main"], work, self._svc_env("release-service"))
        rmtree(work)

    def clone_to(self, project: str, dest: Path, branch: str = "main") -> Path:
        git(["clone", "-q", "-b", branch, str(self.bare(project)), str(dest)], dest.parent)
        return dest

    def head(self, project: str, ref: str = "main") -> str:
        return git(["rev-parse", ref], self.bare(project))

    def push_branch(self, project: str, workdir: Path, branch: str, actor: str, message: str) -> str:
        """Push from a sandbox workspace. No service token: protected branches are rejected by the hook."""
        git(["checkout", "-q", "-B", branch], workdir)
        git(["add", "-A"], workdir)
        if git(["status", "--porcelain"], workdir):
            git(["commit", "-q", "-m", message], workdir, {"GIT_AUTHOR_NAME": actor, "GIT_COMMITTER_NAME": actor})
        git(["push", "-q", "-f", "origin", f"{branch}:{branch}"], workdir)
        sha = git(["rev-parse", "HEAD"], workdir)
        self.ledger.record("repo.pushed", f"{project}:{branch}", actor, {"sha": sha}, project)
        return sha

    def raw_push(self, workdir: Path, refspec: str) -> tuple[bool, str]:
        """What an agent could attempt directly from a sandbox shell (used by tests)."""
        r = subprocess.run([GIT, "push", "origin", refspec], cwd=str(workdir), capture_output=True, **TEXT,
                           env=child_env({"HOME": str(workdir)}, path=None if IS_WINDOWS else "/usr/bin:/bin"))
        return r.returncode == 0, r.stderr

    def changed_files(self, project: str, base: str, head: str) -> list[str]:
        out = git(["diff", "--name-only", f"{base}...{head}"], self.bare(project))
        return [l for l in out.splitlines() if l]

    def diff(self, project: str, base: str, head: str) -> dict[str, str]:
        files = {}
        for f in self.changed_files(project, base, head):
            files[f] = git(["show", f"{head}:{f}"], self.bare(project), check=False)
        return files

    def open_pr(self, project: str, branch: str, title: str, author: str, data: dict | None = None) -> dict:
        head = git(["rev-parse", branch], self.bare(project))
        base = self.head(project)
        pid = new_id("PR")
        self.db.execute("INSERT INTO prs(id,project,branch,head_sha,base,author,status,title,created,data) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (pid, project, branch, head, base, author, "open", title, now(), j(data or {})))
        self.ledger.record("pr.opened", pid, author, {"branch": branch, "head": head, "base": base}, project)
        return {"pr_id": pid, "head_sha": head, "base": base}

    def pr(self, pid: str) -> dict:
        r = self.db.one("SELECT * FROM prs WHERE id=?", (pid,))
        r["data"] = unj(r["data"], {})
        return r

    def merge(self, pid: str, check_review, check_ci) -> str:
        pr = self.pr(pid)
        if pr["status"] != "open":
            raise PermissionError(f"PR {pid} is {pr['status']}")
        current = git(["rev-parse", pr["branch"]], self.bare(pr["project"]))
        if current != pr["head_sha"]:
            raise PermissionError("branch moved after review; re-review required")
        check_review(pr)   # raises if no valid bound engineer approval for head_sha
        check_ci(pr)       # raises if no green trusted CI for head_sha
        work = self.root / f".merge-{pid}"
        rmtree(work, ignore_errors=True)
        self.clone_to(pr["project"], work)
        git(["fetch", "-q", "origin", pr["branch"]], work)
        git(["merge", "-q", "--no-ff", "-m", f"merge {pid}: {pr['title']}", pr["head_sha"]], work, self._svc_env())
        git(["push", "-q", "origin", "main"], work, self._svc_env())
        sha = git(["rev-parse", "HEAD"], work)
        rmtree(work)
        self.db.execute("UPDATE prs SET status='merged', merged_sha=? WHERE id=?", (sha, pid))
        self.ledger.record("pr.merged", pid, "repo-service", {"merged_sha": sha, "head": pr["head_sha"]}, pr["project"])
        return sha

    def archive(self, project: str, sha: str, dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        tar = dest.parent / f"{dest.name}.src.tar"
        git(["archive", "--format=tar", "-o", str(tar), sha], self.bare(project))
        with tarfile.open(tar) as tf:                      # portable: no `tar` binary needed (Windows)
            tf.extractall(dest, filter="data")
        tar.unlink()
        return dest
