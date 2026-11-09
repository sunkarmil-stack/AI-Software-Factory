"""Release bundles and promotion — Pipeline D (REQ 042, 045, 046, 048; NFR 07; ACC 01, 05, 09).

Promotion is executed only by the `release-service` workload after: manifest signature,
provenance verification (trusted builder, subject digest, approved source), evidence
completeness, trace freshness (no stale/superseded dependency), policy evaluation and
bound human approvals for this manifest digest + environment + spec state.
The same artifact digest moves through environments; environments never rebuild it.
"""
from __future__ import annotations

import importlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import gzip
from pathlib import Path

from ..core.approvals import ApprovalError, ApprovalService
from ..core.db import j, unj
from ..core.evidence import EvidenceWriteError
from ..core.util import canonical_json, digest_obj, new_id, now

REQUIRED_CHECKS = ["protected_assets", "dependency_pinning", "base_image", "iac_policy", "secret_scan", "sast",
                   "unit_tests", "acceptance_tests", "contract_tests", "reproducible_build"]
STAGES = [("canary", 10), ("partial", 50), ("full", 100)]


class PromotionDenied(PermissionError):
    pass


class ReleaseService:
    IDENTITY = "release-service"

    def __init__(self, f):
        self.f = f
        self.envs = f.home / "environments"

    # ---------------- packaging (Stage 5) ----------------
    def create(self, project: str, build_id: str, spec_id: str, pr_id: str, created_by: str,
               agent_bundle: dict | None = None) -> dict:
        b = self.f.ci.build(build_id)
        if b["status"] != "passed":
            raise PromotionDenied(f"build {build_id} did not pass")
        spec_row = self.f.specs.current_baseline(spec_id)
        spec = spec_row["body"]
        pr = self.f.repo.pr(pr_id)
        prev = self.f.db.one("SELECT id, manifest FROM releases WHERE project=? AND status='promoted' ORDER BY created DESC LIMIT 1",
                             (project,))
        rid = new_id("REL")
        manifest = {
            "release_id": rid, "project": project, "created": now(), "expires": now() + 14 * 86400,
            "artifact": {"digest": b["artifact_digest"], "ref": f"factory-registry/{project}@{b['artifact_digest']}"},
            "source": {"commit": b["source_sha"], "pr": pr_id, "pr_head": pr["head_sha"]},
            "spec": {"id": spec_id, "version": spec_row["version"], "digest": spec_row["digest"]},
            "contracts": spec["contracts"],
            "agent_bundle": agent_bundle,
            "prompts": (agent_bundle or {}).get("prompts"),
            "tool_contracts": {n: self.f.tools.contract_digest(n) for n in sorted(self.f.tools.contracts)},
            "policy": {"id": self.f.policy.policy_id, "version": self.f.policy.version, "digest": self.f.policy.digest},
            "deployment_config": {"k8s": "deploy/k8s/deployment.yaml", "environments": spec["release_requirements"]["environments"]},
            "evidence": {"build": build_id, "checks": b["report"]["checks"], "sbom_digest": digest_obj(b["report"]["sbom"]),
                         "provenance_digest": digest_obj(b["provenance"]), "acceptance_suite": b["report"]["acceptance_digest"],
                         "limitations": b["report"]["limitations"]},
            "migration_plan": spec["release_requirements"].get("migration", "none: no schema change"),
            "rollback_plan": {"strategy": spec["release_requirements"]["rollback"], "previous_release": prev["id"] if prev else None},
            "secrets": "referenced by workload identity only; none packaged",
        }
        md = digest_obj(manifest)
        sig = self.f.keys.sign(self.IDENTITY, canonical_json(manifest))
        self.f.db.execute("INSERT INTO releases(id,project,manifest,manifest_digest,signature,status,created) VALUES(?,?,?,?,?,?,?)",
                          (rid, project, j(manifest), md, sig, "candidate", now()))
        t = self.f.trace
        rnode = t.node("RELEASE", rid, md, project, {"artifact": b["artifact_digest"]})
        bnode = t.db.one("SELECT id FROM trace_nodes WHERE kind='BUILD' AND ref=?", (build_id,))["id"]
        t.edge(rnode, bnode, "packages")
        snode = t.current("SPEC", f"{spec_id}@v{spec_row['version']}", project)
        if snode:
            t.edge(rnode, snode["id"], "implements_spec")
        self.f.ledger.record("release.created", rid, created_by, {"manifest_digest": md, "artifact": b["artifact_digest"]}, project)
        return self.get(rid)

    def get(self, rid: str) -> dict:
        r = self.f.db.one("SELECT * FROM releases WHERE id=?", (rid,))
        r["manifest"] = unj(r["manifest"])
        return r

    def binding(self, rid: str, env: str) -> dict:
        r = self.get(rid)
        return ApprovalService.binding("release.promote", r["manifest_digest"], env, r["project"],
                                       {"spec_digest": r["manifest"]["spec"]["digest"]})

    def link_approval(self, rid: str, aid: str) -> None:
        t = self.f.trace
        rn = t.db.one("SELECT id FROM trace_nodes WHERE kind='RELEASE' AND ref=?", (rid,))["id"]
        an = t.node("APPROVAL", aid, digest_obj(aid), self.get(rid)["project"])
        t.edge(an, rn, "authorizes")

    def evidence_summary(self, rid: str) -> dict:
        r = self.get(rid)
        m = r["manifest"]
        rn = self.f.db.one("SELECT id FROM trace_nodes WHERE kind='RELEASE' AND ref=?", (rid,))["id"]
        return {"manifest_digest": r["manifest_digest"], "checks": {c: m["evidence"]["checks"].get(c, "missing") for c in REQUIRED_CHECKS},
                "stale": self.f.trace.stale_in_closure(rn), "limitations": m["evidence"]["limitations"]}

    # ---------------- promotion (Stage 6) ----------------
    def promote(self, rid: str, env: str, approval_ids: list[str], requested_by: str,
                health_check=None) -> dict:
        r = self.get(rid)
        m, project = r["manifest"], r["project"]
        problems = []
        if not self.f.keys.verify(self.IDENTITY, canonical_json(m), r["signature"]) or digest_obj(m) != r["manifest_digest"]:
            problems.append("release manifest signature/digest invalid")
        if now() > m["expires"]:
            problems.append("release manifest expired")
        build = self.f.ci.build(m["evidence"]["build"])
        pr = self.f.repo.pr(m["source"]["pr"])
        if pr["status"] != "merged" or pr["merged_sha"] != m["source"]["commit"]:
            problems.append("artifact source is not the merged, reviewed revision")
        problems += self.f.ci.verify_provenance(build, m["source"]["commit"])
        if build["artifact_digest"] != m["artifact"]["digest"]:
            problems.append("manifest artifact digest != built artifact")
        for c in REQUIRED_CHECKS:
            if m["evidence"]["checks"].get(c) != "passed":
                problems.append(f"required check {c} not passed")
        baseline = self.f.specs.current_baseline(m["spec"]["id"])
        if not baseline or baseline["digest"] != m["spec"]["digest"]:
            problems.append("release implements a superseded specification baseline")
        stale = self.evidence_summary(rid)["stale"]
        if stale:
            problems.append(f"stale evidence in release trace: {stale[:6]}")
        facts = {"actor": {"kind": "workload", "id": self.IDENTITY}, "action": "deploy.execute", "side_effect": "write",
                 "env": env, "reversible": True, "tool_registered": True, "grant": True, "tenant_match": True}
        dec = self.f.policy.evaluate(facts)
        if not dec.allow:
            problems.append(f"policy: {dec.reasons}")
        roles = dec.obligations.get("approval_roles", ["release_owner"])
        used = []
        if not problems:
            try:
                used = self.f.approvals.require(approval_ids, self.binding(rid, env), roles)
                authors = {pr["author"], requested_by, self.f.db.one("SELECT created_by FROM specs WHERE spec_id=? AND version=?",
                                                                     (m["spec"]["id"], m["spec"]["version"]))["created_by"]}
                for a in used:
                    if self.f.approvals.get(a)["approver"] in authors:
                        raise ApprovalError("release approver must be independent of the change author/requester")
            except ApprovalError as e:
                problems.append(str(e))
        if problems:
            self.f.ledger.record("release.promotion_denied", rid, self.IDENTITY, {"env": env, "problems": problems}, project)
            self.f.tm.metric("delivery", "promotion_denied", 1, env=env, category="validation_failed")
            raise PromotionDenied("; ".join(problems))
        # evidence must be durably written BEFORE the consequential action (NFR 07)
        try:
            self.f.ledger.record("release.promotion_authorized", rid, self.IDENTITY,
                                 {"env": env, "approvals": used, "policy": dec.policy_digest}, project)
        except EvidenceWriteError as e:
            raise PromotionDenied(f"evidence write failed; promotion not attempted: {e}")
        return self._rollout(rid, env, m, used, health_check)

    def _deploy_dir(self, project: str, env: str) -> Path:
        return self.envs / env / project

    def _materialize(self, project: str, env: str, digest: str) -> Path:
        dest = self._deploy_dir(project, env) / digest.split(":")[1][:16]
        if not dest.exists():
            blob = (self.f.ci.registry / f"{digest.split(':')[1]}.tar.gz").read_bytes()
            dest.mkdir(parents=True)
            with tarfile.open(fileobj=io.BytesIO(gzip.decompress(blob))) as tf:
                tf.extractall(dest, filter="data")
        return dest

    def smoke(self, path: Path, spec: dict) -> list[str]:
        """Post-deploy verification against the running artifact using approved ACs (no side effects to prod data)."""
        checks = [(ac["id"], ac["check"]) for r in spec["requirements"] for ac in r["acceptance_criteria"]
                  if ac["check"]["request"]["method"] == "GET"]
        code = ("import json,sys;sys.path.insert(0,'.');from app.http import dispatch;import app.routes as r;"
                "cs=json.loads(sys.argv[1]);out={}\n"
                "for i,c in cs:\n q=c['request'];s,b=dispatch(r.ROUTES,q['method'],q['path'],q.get('headers',{}),q.get('body'))\n"
                " out[i]=s==c['expect']['status']\nprint(json.dumps(out))")
        res = subprocess.run([sys.executable, "-I", "-c", code, json.dumps(checks)], cwd=str(path), capture_output=True,
                             text=True, timeout=60)
        if res.returncode != 0:
            return [f"service failed to start: {res.stderr[-300:]}"]
        return [k for k, ok in json.loads(res.stdout).items() if not ok]

    def _rollout(self, rid: str, env: str, m: dict, approvals: list[str], health_check) -> dict:
        project, digest = m["project"], m["artifact"]["digest"]
        spec = self.f.specs.get(m["spec"]["id"], m["spec"]["version"])["body"]
        did = new_id("DEP")
        prev = self.f.db.one("SELECT * FROM deployments WHERE project=? AND env=? AND status='active' ORDER BY created DESC LIMIT 1",
                             (project, env))
        path = self._materialize(project, env, digest)
        desired = self._deploy_dir(project, env) / "desired.json"
        stages_done = []
        for stage, pct in STAGES:
            desired.write_text(json.dumps({"release": rid, "digest": digest, "stage": stage, "weight": pct,
                                           "previous": prev["artifact_digest"] if prev else None}))
            failures = self.smoke(path, spec)
            if health_check:
                failures += health_check(stage, pct)
            self.f.ledger.record("deploy.stage", did, self.IDENTITY, {"stage": stage, "weight": pct, "failures": failures}, project)
            if failures:
                if prev:
                    desired.write_text(json.dumps({"release": prev["release_id"], "digest": prev["artifact_digest"],
                                                   "stage": "rolled_back", "weight": 100}))
                self.f.db.execute("INSERT INTO deployments(id,release_id,project,env,artifact_digest,status,data,created) VALUES(?,?,?,?,?,?,?,?)",
                                  (did, rid, project, env, digest, "rolled_back", j({"failed_stage": stage, "failures": failures,
                                                                                     "stages": stages_done}), now()))
                self.f.ledger.record("deploy.rolled_back", did, self.IDENTITY, {"stage": stage, "failures": failures,
                                                                                 "restored": prev["artifact_digest"] if prev else None}, project)
                self.f.tm.metric("delivery", "rollbacks", 1, env=env, category="application_error")
                return {"deployment_id": did, "status": "rolled_back", "failed_stage": stage, "failures": failures}
            stages_done.append(stage)
        if prev:
            self.f.db.execute("UPDATE deployments SET status='superseded' WHERE id=?", (prev["id"],))
        self.f.db.execute("INSERT INTO deployments(id,release_id,project,env,artifact_digest,status,data,created) VALUES(?,?,?,?,?,?,?,?)",
                          (did, rid, project, env, digest, "active", j({"stages": stages_done, "approvals": approvals}), now()))
        self.f.db.execute("UPDATE releases SET status='promoted' WHERE id=?", (rid,))
        t = self.f.trace
        dn = t.node("DEPLOY", did, digest_obj([did, digest]), project, {"env": env})
        rn = t.db.one("SELECT id FROM trace_nodes WHERE kind='RELEASE' AND ref=?", (rid,))["id"]
        t.edge(dn, rn, "deploys")
        self.f.ledger.record("deploy.completed", did, self.IDENTITY, {"env": env, "digest": digest, "release": rid}, project)
        self.f.tm.metric("delivery", "deployments", 1, env=env)
        return {"deployment_id": did, "status": "active", "stages": stages_done, "path": str(path)}

    def drift(self, project: str, env: str) -> list[str]:
        """Pipeline E: compare desired state with what is materialized (REQ 043 drift detection)."""
        dep = self.f.db.one("SELECT * FROM deployments WHERE project=? AND env=? AND status='active' ORDER BY created DESC LIMIT 1",
                            (project, env))
        if not dep:
            return []
        out = []
        desired = json.loads((self._deploy_dir(project, env) / "desired.json").read_text())
        if desired["digest"] != dep["artifact_digest"]:
            out.append(f"desired digest {desired['digest']} != recorded deployment {dep['artifact_digest']}")
        path = self._deploy_dir(project, env) / dep["artifact_digest"].split(":")[1][:16]
        from .ci import deterministic_tar, BUILD_INPUTS
        from ..core.util import sha256_bytes
        if sha256_bytes(deterministic_tar(path, BUILD_INPUTS)) != dep["artifact_digest"]:
            out.append("runtime files differ from approved artifact (out-of-band change)")
        return out
