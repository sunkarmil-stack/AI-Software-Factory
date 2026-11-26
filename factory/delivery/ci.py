"""Trusted CI — Pipeline B (REQ 035, 036, 041, 042, 043, 044; Section 9).

Runs as the `ci-builder` workload identity on the exact reviewed revision, independent of
any agent sandbox. Protected acceptance tests come from the QA-owned store (bound to a QA
approval), never from the branch under test. PR content and test logs are untrusted input.
"""
from __future__ import annotations

import ast
import base64
import gzip
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

from ..core.platform import rmtree
from ..core.approvals import ApprovalService
from ..core.util import canonical_json, digest_obj, iso, iter_files, new_id, now, sha256_bytes
from ..core.db import j, unj
from ..agents.codegen import generate_tests

SECRET_RE = [re.compile(p) for p in [r"AKIA[0-9A-Z]{16}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
                                     r"(?i)(password|secret|api_key|token)\s*=\s*['\"][A-Za-z0-9/+_\-]{8,}['\"]",
                                     r"xox[baprs]-[0-9A-Za-z-]{10,}"]]
BUILD_INPUTS = ["app", "openapi.json", "requirements.lock", "Dockerfile", "deploy", "pyproject.toml"]
CI_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
          "http_proxy": "http://127.0.0.1:9", "https_proxy": "http://127.0.0.1:9"}


class QAStore:
    """QA-owned protected acceptance assets (REQ 035). Generating agents have no write path here."""

    def __init__(self, f):
        self.f = f
        self.root = f.home / "protected"

    def path(self, project: str, spec_id: str, version: int) -> Path:
        return self.root / project / f"{spec_id}@v{version}" / "test_acceptance.py"

    def materialize(self, spec: dict, qa_owner: str) -> dict:
        src = generate_tests(spec, "PROTECTED acceptance tests (QA-owned; do not edit via implementation branches)", "test")
        d = sha256_bytes(src.encode())
        aid = self.f.approvals.approve(qa_owner, "qa_owner", ApprovalService.binding(
            "acceptance.publish", d, "ci", f"{spec['spec_id']}@v{spec['version']}"), rationale="acceptance suite from approved ACs")
        p = self.path(spec["project"], spec["spec_id"], spec["version"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
        (p.parent / "approval.json").write_text(json.dumps({"approval": aid, "digest": d}), encoding="utf-8")
        self.f.ledger.record("qa.acceptance_published", f"{spec['spec_id']}@v{spec['version']}", qa_owner,
                             {"digest": d, "approval": aid}, spec["project"])
        return {"digest": d, "approval": aid}

    def load(self, project: str, spec_id: str, version: int) -> tuple[str, str]:
        p = self.path(project, spec_id, version)
        if not p.exists():
            raise PermissionError("no QA-approved acceptance suite for this spec version")
        meta = json.loads((p.parent / "approval.json").read_text(encoding="utf-8"))
        src = p.read_text(encoding="utf-8")
        d = sha256_bytes(src.encode())
        chk = self.f.approvals.check(meta["approval"], ApprovalService.binding(
            "acceptance.publish", d, "ci", f"{spec_id}@v{version}"), "qa_owner")
        if not chk.ok:
            raise PermissionError(f"protected acceptance suite integrity: {chk.reason}")
        return src, d


def deterministic_tar(src: Path, inputs: list[str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        files = []
        for name in inputs:
            p = src / name
            if p.is_file():
                files.append(p)
            elif p.is_dir():
                files += [x for x in iter_files(p)]
        for p in sorted(files, key=lambda x: x.relative_to(src).as_posix()):
            ti = tarfile.TarInfo(p.relative_to(src).as_posix())
            data = p.read_bytes()
            ti.size, ti.mtime, ti.mode, ti.uid, ti.gid, ti.uname, ti.gname = len(data), 0, 0o644, 0, 0, "", ""
            tf.addfile(ti, io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0, compresslevel=9) as gz:
        gz.write(buf.getvalue())
    return out.getvalue()


def sast(src: Path) -> list[str]:
    findings = []
    for p in sorted(src.rglob("*.py")):
        rel = p.relative_to(src).as_posix()
        if rel.startswith("tests/"):
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"), rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = ast.unparse(node.func)
                if name in ("eval", "exec", "pickle.loads", "marshal.loads", "os.system"):
                    findings.append(f"{rel}:{node.lineno} dangerous call {name}")
                if name.startswith("subprocess.") and any(k.arg == "shell" and getattr(k.value, "value", False)
                                                          for k in node.keywords):
                    findings.append(f"{rel}:{node.lineno} subprocess shell=True")
                if name == "yaml.load" and not any(k.arg == "Loader" for k in node.keywords):
                    findings.append(f"{rel}:{node.lineno} yaml.load without SafeLoader")
        if rel == "app/routes.py":
            funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and ast.unparse(node.func) == "Route" and len(node.args) >= 5:
                    handler, scope = ast.unparse(node.args[3]), node.args[4].value
                    fn = funcs.get(handler)
                    calls = {ast.unparse(c.func) for c in ast.walk(fn) if isinstance(c, ast.Call)} if fn else set()
                    if scope != "public" and "require_scope" not in calls:
                        findings.append(f"{rel}: handler {handler} for scope {scope} has no require_scope (missing authorization)")
    return findings


def iac_policy(src: Path) -> list[str]:
    out = []
    for p in sorted((src / "deploy").rglob("*.yaml")) if (src / "deploy").exists() else []:
        for doc in yaml.safe_load_all(p.read_text(encoding="utf-8")):
            if not doc:
                continue
            kind = doc.get("kind")
            if kind == "Deployment":
                spec = doc["spec"]["template"]["spec"]
                if not spec.get("securityContext", {}).get("runAsNonRoot"):
                    out.append(f"{p.name}: runAsNonRoot must be true")
                if spec.get("hostNetwork"):
                    out.append(f"{p.name}: hostNetwork forbidden")
                for c in spec["containers"]:
                    sc = c.get("securityContext", {})
                    if sc.get("privileged") or sc.get("allowPrivilegeEscalation", True):
                        out.append(f"{p.name}:{c['name']} privilege escalation must be disabled")
                    if not sc.get("readOnlyRootFilesystem"):
                        out.append(f"{p.name}:{c['name']} readOnlyRootFilesystem required")
                    if not c.get("resources", {}).get("limits"):
                        out.append(f"{p.name}:{c['name']} resource limits required")
                    if str(c.get("image", "")).endswith(":latest"):
                        out.append(f"{p.name}:{c['name']} mutable image tag")
            if kind == "Service" and doc["spec"].get("type") == "LoadBalancer" and \
                    "internal" not in json.dumps(doc["metadata"].get("annotations", {})):
                out.append(f"{p.name}: public LoadBalancer endpoint prohibited")
    for p in sorted((src / "infra").rglob("*.tf")) if (src / "infra").exists() else []:
        t = p.read_text(encoding="utf-8")
        if re.search(r"public_network_access_enabled\s*=\s*true", t):
            out.append(f"{p.name}: public network access prohibited")
        for m in re.finditer(r'source\s*=\s*"[^"]+",\s*version\s*=\s*"([^"]+)"', t):
            if not m.group(1).startswith("="):
                out.append(f"{p.name}: provider version must be exactly pinned ({m.group(1)})")
        if re.search(r'role_definition_name\s*=\s*"(Owner|Contributor)"', t):
            out.append(f"{p.name}: excessive role assignment")
    return out


class TrustedCI:
    IDENTITY = "ci-builder"

    def __init__(self, f):
        self.f = f
        self.root = f.home / "ci"
        self.registry = f.home / "registry"
        self.registry.mkdir(parents=True, exist_ok=True)
        self.qa = QAStore(f)
        self.standards = yaml.safe_load((f.catalog / "build-standards.yaml").read_text(encoding="utf-8"))

    def _py(self, cwd: Path, args: list[str], timeout=300) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, *args], cwd=str(cwd), env={**CI_ENV, "HOME": str(cwd), "PYTHONPATH": str(cwd)},
                              capture_output=True, text=True, timeout=timeout)

    def run(self, project: str, sha: str, spec: dict, base: str | None = None, change_ref: str | None = None) -> dict:
        bid = new_id("BLD")
        work = self.root / bid
        src = self.f.repo.archive(project, sha, work / "src")
        checks: dict[str, str] = {}
        details: dict[str, object] = {}
        limitations = []

        def check(name: str, problems: list[str]):
            checks[name] = "passed" if not problems else "failed"
            if problems:
                details[name] = problems[:20]

        # protected assets: branch may not touch QA/eval/policy paths
        if base:
            changed = self.f.repo.changed_files(project, base, sha)
            check("protected_assets", [f"protected asset modified: {c}" for c in changed
                                       if any(c.startswith(pp) for pp in self.standards["protected_paths"])])
        else:
            checks["protected_assets"] = "passed"
        lock = (src / "requirements.lock").read_text(encoding="utf-8").splitlines() if (src / "requirements.lock").exists() else []
        deps = [l.strip() for l in lock if l.strip() and not l.startswith("#")]
        check("dependency_pinning", [f"unpinned dependency {d}" for d in deps if "==" not in d])
        if not any("--hash=" in d for d in deps):
            limitations.append("dependency hashes not enforced: enterprise package mirror not configured")
        df = (src / "Dockerfile").read_text(encoding="utf-8") if (src / "Dockerfile").exists() else ""
        check("base_image", [] if "ARG BASE_IMAGE" in df and "FROM ${BASE_IMAGE}" in df else
              ["Dockerfile must take BASE_IMAGE from trusted CI (approved list)"])
        check("iac_policy", iac_policy(src))
        check("secret_scan", [f"{p.relative_to(src)}: {r.pattern}" for p in iter_files(src)
                              for r in SECRET_RE if r.search(p.read_text(errors="ignore", encoding="utf-8"))])
        check("sast", sast(src))
        # tests: unit (branch) + protected acceptance (QA store)
        acc_src, acc_digest = self.qa.load(project, spec["spec_id"], spec["version"])
        rmtree(src / "tests" / "acceptance", ignore_errors=True)
        (src / "tests" / "acceptance").mkdir(parents=True)
        (src / "tests" / "acceptance" / "test_protected.py").write_text(acc_src, encoding="utf-8")
        unit = self._py(src, ["-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/unit"]) if (src / "tests" / "unit").exists() else None
        check("unit_tests", [] if unit is None or unit.returncode == 0 else [unit.stdout[-1500:]])
        acc = self._py(src, ["-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/acceptance", f"--junitxml={work}/acc.xml"])
        results = {}
        if (work / "acc.xml").exists():
            for tc in ET.parse(work / "acc.xml").getroot().iter("testcase"):
                failed = tc.find("failure") is not None or tc.find("error") is not None
                m = re.match(r"test_ac_(\d+)", tc.get("name", ""))
                if m:
                    results[f"AC-{m.group(1)}"] = "failed" if failed else "passed"
        check("acceptance_tests", [] if acc.returncode == 0 else [f"{k} failed" for k, v in results.items() if v != "passed"]
              or [acc.stdout[-1500:]])
        details["acceptance_results"] = results
        # contract tests
        rt = self._py(src, ["-c", "import json,app.routes as r;print(json.dumps([[x.method,x.path,x.operation_id,x.required_scope] for x in r.ROUTES]))"])
        problems = []
        if rt.returncode != 0:
            problems.append(f"routes import failed: {rt.stderr[-500:]}")
        else:
            actual = {tuple(x) for x in json.loads(rt.stdout.strip().splitlines()[-1])}
            for c in spec["contracts"]:
                for ep in c["endpoints"]:
                    want = (ep["method"].upper(), ep["path"], ep["operation_id"], ep["required_scope"])
                    if want not in actual:
                        problems.append(f"contract {c['id']}@{c['version']}: missing/incorrect {want}")
            oa = json.loads((src / "openapi.json").read_text(encoding="utf-8")) if (src / "openapi.json").exists() else {"paths": {}}
            for c in spec["contracts"]:
                for ep in c["endpoints"]:
                    op = oa["paths"].get(ep["path"], {}).get(ep["method"].lower())
                    if not op or op.get("x-require-mfa") != bool(ep.get("require_mfa")):
                        problems.append(f"openapi.json out of date for {ep['operation_id']}")
            prev = self.f.db.one("SELECT manifest FROM releases WHERE project=? AND status IN ('released','promoted') ORDER BY created DESC LIMIT 1", (project,))
            if prev:
                pm = unj(prev["manifest"])
                for oc in pm.get("contracts", []):
                    nc = next((c for c in spec["contracts"] if c["id"] == oc["id"]), None)
                    if not nc:
                        problems.append(f"contract {oc['id']} removed"); continue
                    old_ops = {e["operation_id"] for e in oc["endpoints"]}
                    new_ops = {e["operation_id"] for e in nc["endpoints"]}
                    if old_ops - new_ops and nc["version"] <= oc["version"]:
                        problems.append(f"breaking change without version bump: removed {sorted(old_ops - new_ops)}")
        check("contract_tests", problems)
        # build twice -> byte reproducible (hermetic template)
        a1, a2 = deterministic_tar(src, BUILD_INPUTS), deterministic_tar(src, BUILD_INPUTS)
        artifact_digest = sha256_bytes(a1)
        check("reproducible_build", [] if a1 == a2 else ["artifact bytes differ between two builds"])
        sbom = {"bomFormat": "CycloneDX", "specVersion": "1.5", "metadata": {"component": {"name": project, "version": sha[:12]}},
                "components": [{"type": "library", "name": d.split("==")[0], "version": d.split("==")[1] if "==" in d else "UNPINNED",
                                "purl": f"pkg:pypi/{d.split('==')[0]}@{d.split('==')[1] if '==' in d else ''}"} for d in deps]}
        status = "passed" if all(v == "passed" for v in checks.values()) else "failed"
        prov_env = None
        if status == "passed":
            (self.registry / f"{artifact_digest.split(':')[1]}.tar.gz").write_bytes(a1)
            statement = {
                "_type": "https://in-toto.io/Statement/v1",
                "subject": [{"name": f"{project}.tar.gz", "digest": {"sha256": artifact_digest.split(":")[1]}}],
                "predicateType": "https://slsa.dev/provenance/v1",
                "predicate": {"buildDefinition": {"buildType": "factory://pipeline-b/python-service@v1",
                                                  "externalParameters": {"source": {"repo": f"factory://repos/{project}", "commit": sha},
                                                                         "spec": f"{spec['spec_id']}@v{spec['version']}"},
                                                  "resolvedDependencies": [{"uri": f"git+factory://repos/{project}", "digest": {"gitCommit": sha}},
                                                                           {"uri": "factory://protected/acceptance", "digest": {"sha256": acc_digest.split(':')[1]}}]},
                              "runDetails": {"builder": {"id": "factory://builders/ci-builder"},
                                             "metadata": {"invocationId": bid, "startedOn": iso()}}}}
            payload = canonical_json(statement)
            prov_env = {"payloadType": "application/vnd.in-toto+json", "payload": base64.b64encode(payload).decode(),
                        "signatures": [{"keyid": self.IDENTITY, "sig": self.f.keys.sign(self.IDENTITY, payload)}]}
        report = {"checks": checks, "details": details, "limitations": limitations, "sbom": sbom,
                  "acceptance_digest": acc_digest, "spec": f"{spec['spec_id']}@v{spec['version']}"}
        self.f.db.execute("INSERT INTO builds(id,project,source_sha,artifact_digest,provenance,status,report,created) VALUES(?,?,?,?,?,?,?,?)",
                          (bid, project, sha, artifact_digest if status == "passed" else None, j(prov_env), status, j(report), now()))
        self.f.ledger.record("ci.build", bid, self.IDENTITY, {"sha": sha, "status": status, "checks": checks,
                                                              "artifact": artifact_digest, "limitations": limitations}, project)
        self.f.tm.metric("delivery", "ci_builds", 1, status=status, **({} if status == "passed" else {"category": "validation_failed"}))
        if status == "passed":
            t = self.f.trace
            bnode = t.node("BUILD", bid, artifact_digest, project, {"sha": sha})
            for ac_id, res in results.items():
                acn = t.current("AC", ac_id, project)
                if acn and res == "passed":
                    tn = t.node("TEST", f"{ac_id}@{sha[:12]}", digest_obj([acn["digest"], sha]), project, {"build": bid})
                    t.edge(tn, acn["id"], "verifies")
                    t.edge(bnode, tn, "validated_by")
            if change_ref:
                t.edge(bnode, change_ref, "built_from")
        rmtree(work / "src", ignore_errors=True)
        return self.build(bid)

    def build(self, bid: str) -> dict:
        r = self.f.db.one("SELECT * FROM builds WHERE id=?", (bid,))
        r["report"], r["provenance"] = unj(r["report"]), unj(r["provenance"])
        return r

    def verify_provenance(self, build: dict, expected_sha: str) -> list[str]:
        env = build.get("provenance")
        if not env:
            return ["no provenance"]
        payload = base64.b64decode(env["payload"])
        probs = []
        sig = env["signatures"][0]
        if sig["keyid"] != self.IDENTITY or not self.f.keys.verify(self.IDENTITY, payload, sig["sig"]):
            probs.append("provenance signature not from trusted builder")
        st = json.loads(payload)
        if st["predicate"]["runDetails"]["builder"]["id"] != "factory://builders/ci-builder":
            probs.append("untrusted builder id")
        if "sha256:" + st["subject"][0]["digest"]["sha256"] != build["artifact_digest"]:
            probs.append("attestation subject does not match artifact digest")
        if st["predicate"]["buildDefinition"]["externalParameters"]["source"]["commit"] != expected_sha:
            probs.append("attested source commit is not the approved revision")
        blob = self.registry / f"{build['artifact_digest'].split(':')[1]}.tar.gz"
        if not blob.exists() or sha256_bytes(blob.read_bytes()) != build["artifact_digest"]:
            probs.append("registry artifact missing or altered")
        return probs
