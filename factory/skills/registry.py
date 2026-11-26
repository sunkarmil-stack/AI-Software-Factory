"""Skill registry (REQ 013-017, ACC 03).

A skill is versioned task knowledge (instructions + optional schemas/templates/assets).
Publishing runs static checks, behavior tests and (risk-appropriate) security approval,
then signs the content digest. Loading re-verifies digest and signature against the
registry record. Loading a skill never grants permissions (REQ 014).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..core.platform import IS_WINDOWS, TEXT, child_env, rmtree
from ..core.approvals import ApprovalService
from ..core.crypto import KeyStore
from ..core.db import DB, j, unj
from ..core.evidence import Ledger
from ..core.util import digest_dir, now

REQUIRED_MANIFEST = ["name", "version", "owner", "description", "compatibility", "inputs", "outputs",
                     "requested_capabilities", "data_classes", "side_effects", "network", "tests",
                     "limitations", "risk"]

INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above) instructions",
    r"you (are|have been) (now )?(authori[sz]ed|granted|elevated)",
    r"(disable|bypass|skip|override) (the )?(policy|guardrail|approval|review|safety)",
    r"approve (your|this) own",
    r"\bexfiltrate (the|all|any|every)\b",
    r"(print|reveal|send) .*(secret|credential|private key|token)",
    r"act as (an? )?(admin|administrator|release owner|root)",
]
NETWORK_PATTERNS = [r"\bimport (requests|httpx|urllib3|socket|aiohttp)\b", r"\bfrom (requests|httpx|socket|urllib)\b",
                    r"urllib\.request", r"\bcurl\b", r"\bwget\b", r"socket\.socket", r"https?://(?!example\.)"]
SECTIONS = ["## Inputs", "## Outputs", "## Procedure", "## Limitations"]


class SkillRejected(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


class SkillIntegrityError(PermissionError):
    pass


@dataclass
class LoadedSkill:
    name: str
    version: str
    digest: str
    manifest: dict
    instructions: str
    path: Path


class SkillRegistry:
    def __init__(self, db: DB, ledger: Ledger, keys: KeyStore, approvals: ApprovalService, store: Path,
                 allowed_capabilities: set[str]):
        self.db, self.ledger, self.keys, self.approvals = db, ledger, keys, approvals
        self.store = Path(store); self.store.mkdir(parents=True, exist_ok=True)
        self.allowed = allowed_capabilities

    # -------- static + behavior checks --------
    def scan(self, pkg: Path) -> tuple[dict, list[str]]:
        problems = []
        mf = pkg / "skill.yaml"
        if not mf.exists():
            return {}, ["missing skill.yaml"]
        m = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
        for f in REQUIRED_MANIFEST:
            if f not in m:
                problems.append(f"manifest missing {f}")
        instr = (pkg / "SKILL.md").read_text(encoding="utf-8") if (pkg / "SKILL.md").exists() else ""
        if not instr:
            problems.append("missing SKILL.md instructions")
        for s in SECTIONS:
            if s not in instr:
                problems.append(f"SKILL.md missing section {s}")
        for p in sorted(pkg.rglob("*")):
            if not p.is_file():
                continue
            text = p.read_text(errors="ignore", encoding="utf-8")
            for pat in INJECTION_PATTERNS:
                if re.search(pat, text, re.I):
                    problems.append(f"malicious/unsafe instruction pattern '{pat}' in {p.relative_to(pkg)}")
            if p.suffix in {".py", ".sh", ".js"} and not m.get("network"):
                for pat in NETWORK_PATTERNS:
                    if re.search(pat, text):
                        problems.append(f"undeclared network access '{pat}' in {p.relative_to(pkg)}")
        for c in m.get("requested_capabilities", []):
            if c not in self.allowed:
                problems.append(f"requested capability {c} is not a catalogued capability")
        if m.get("network"):
            problems.append("network-enabled skills are excluded from the MVP")
        problems += self._behavior_tests(pkg, m)
        return m, problems

    def _behavior_tests(self, pkg: Path, m: dict) -> list[str]:
        out = []
        runner = pkg / "assets" / "skill.py"
        for t in m.get("tests", []):
            if runner.exists():
                code = (f"import json,sys,runpy;mod=runpy.run_path({str(runner)!r});"
                        f"print(json.dumps(mod['run'](json.loads(sys.argv[1]))))")
                try:
                    r = subprocess.run([sys.executable, "-I", "-c", code, j(t.get("input", {}))], capture_output=True,
                                       timeout=20, env=child_env(path=None if IS_WINDOWS else "/usr/bin:/bin"),
                                       cwd=str(pkg), **TEXT)
                except subprocess.TimeoutExpired:
                    out.append(f"behavior test {t['name']} timed out"); continue
                if r.returncode != 0:
                    out.append(f"behavior test {t['name']} crashed: {r.stderr[-300:]}"); continue
                res = unj(r.stdout.strip().splitlines()[-1])
                for k, v in (t.get("expect") or {}).items():
                    if res.get(k) != v:
                        out.append(f"behavior test {t['name']}: {k}={res.get(k)!r} expected {v!r}")
            else:
                for k in t.get("expect_sections", []):
                    if k not in (pkg / "SKILL.md").read_text(encoding="utf-8"):
                        out.append(f"behavior test {t['name']}: missing {k}")
        return out

    # -------- publication --------
    def publish(self, pkg: Path, actor: str, security_approval: str | None = None) -> dict:
        pkg = Path(pkg)
        m, problems = self.scan(pkg)
        if problems:
            self.ledger.record("skill.rejected", f"{m.get('name')}@{m.get('version')}", actor, {"problems": problems})
            raise SkillRejected(problems)
        digest = digest_dir(pkg)
        existing = self.db.one("SELECT * FROM skills WHERE name=? AND version=?", (m["name"], str(m["version"])))
        if existing:
            if existing["digest"] == digest:
                return existing
            self.ledger.record("skill.tamper_attempt", f"{m['name']}@{m['version']}", actor,
                               {"registered": existing["digest"], "offered": digest})
            raise SkillRejected([f"version {m['version']} already registered with different content; bump version"])
        if m["risk"] in ("medium", "high"):
            if not security_approval:
                raise SkillRejected([f"risk {m['risk']} requires security review approval"])
            self.approvals.require([security_approval], ApprovalService.binding(
                "skill.publish", digest, "registry", f"{m['name']}@{m['version']}"), ["security"])
        dest = self.store / f"{m['name']}@{m['version']}"
        if dest.exists():
            rmtree(dest)
        shutil.copytree(pkg, dest, ignore=shutil.ignore_patterns("__pycache__"))
        sig = self.keys.sign("skill-registry", f"{m['name']}@{m['version']}|{digest}".encode())
        self.db.execute("INSERT INTO skills(name,version,digest,signature,manifest,path,status,owner,published)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (m["name"], str(m["version"]), digest, sig, j(m), str(dest), "approved", m["owner"], now()))
        self.ledger.record("skill.published", f"{m['name']}@{m['version']}", actor, {"digest": digest})
        return self.db.one("SELECT * FROM skills WHERE name=? AND version=?", (m["name"], str(m["version"])))

    def load(self, name: str, version: str, expected_digest: str | None, run_id: str) -> LoadedSkill:
        r = self.db.one("SELECT * FROM skills WHERE name=? AND version=?", (name, version))
        if not r:
            raise SkillIntegrityError(f"skill {name}@{version} not registered")
        if r["status"] != "approved":
            raise SkillIntegrityError(f"skill {name}@{version} is {r['status']}")
        if expected_digest and expected_digest != r["digest"]:
            raise SkillIntegrityError(f"pinned digest {expected_digest} != registry {r['digest']}")
        actual = digest_dir(Path(r["path"]))
        sig_ok = self.keys.verify("skill-registry", f"{name}@{version}|{r['digest']}".encode(), r["signature"])
        if actual != r["digest"] or not sig_ok:
            self.ledger.record("skill.integrity_failure", f"{name}@{version}", "skill-registry",
                               {"registered": r["digest"], "actual": actual, "signature_ok": sig_ok,
                                "run": run_id, "affected_runs": self.affected_runs(name, version)})
            raise SkillIntegrityError(f"skill {name}@{version} content digest/signature mismatch — rejected")
        self.ledger.record("skill.loaded", f"{name}@{version}", "skill-registry", {"digest": r["digest"], "run": run_id})
        path = Path(r["path"])
        return LoadedSkill(name, version, r["digest"], unj(r["manifest"]), (path / "SKILL.md").read_text(encoding="utf-8"), path)

    def affected_runs(self, name: str, version: str) -> list[str]:
        return sorted({e["data"].get("run") for e in self.ledger.query(subject=f"{name}@{version}", kind="skill.loaded")})

    def revoke(self, name: str, version: str, actor: str, reason: str) -> dict:
        self.db.execute("UPDATE skills SET status='revoked' WHERE name=? AND version=?", (name, version))
        dependents = [a["id"] for a in self.db.all("SELECT id, manifest FROM agents")
                      if f"{name}@{version}" in [f"{s['name']}@{s['version']}" for s in unj(a["manifest"]).get("skills", [])]]
        self.ledger.record("skill.revoked", f"{name}@{version}", actor,
                           {"reason": reason, "dependent_bundles": dependents, "affected_runs": self.affected_runs(name, version)})
        return {"dependent_bundles": dependents}

    def list(self) -> list[dict]:
        return self.db.all("SELECT name,version,digest,status,owner FROM skills ORDER BY name")
