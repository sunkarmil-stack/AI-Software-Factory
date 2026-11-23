"""Factory workflows: Pipeline A (specification), Stage 3 delivery, Stages 4-6 release.

Each step is small and checkpointed by the engine. Human gates are approval steps whose
bindings are computed from the exact state they authorize.
"""
from __future__ import annotations

from pathlib import Path

from .core.approvals import ApprovalService
from .core.util import digest_obj
from .orchestrator.engine import RunContext, Step, StepBlocked, Workflow
from .specs.registry import baseline_roles
from .tools.gateway import CallContext


def _spec(ctx: RunContext) -> dict:
    s = ctx.engine.f.specs.get(ctx.input["spec_id"], ctx.input["version"])
    if not s:
        raise StepBlocked("specification version not found")
    return s["body"]


# ======================= Pipeline A — specification =======================
def a_validate(ctx):
    f = ctx.engine.f
    res = f.specs.submit(ctx.input["spec"], ctx.actor)
    if res.status == "blocked":
        raise StepBlocked(f"spec blocked: missing={res.missing} errors={res.errors[:3]} review_items={res.review_items}")
    findings = f.ontology.check_spec(ctx.input["spec"])
    errors = [x for x in findings if x["severity"] == "error"]
    if errors:
        raise StepBlocked(f"spec conflicts with the approved ontology: {errors}")
    return {"digest": res.digest, "unresolved_decisions": res.unresolved_decisions, "ontology_findings": findings}


def a_analyze(ctx):
    r = ctx.engine.f.agent("specification").run(ctx, {"kind": "spec.analyze", "spec": _spec(ctx)})
    if r.status != "success":
        raise StepBlocked(f"specification agent needs clarification: {r.questions or r.summary}")
    return {"summary": r.summary, "evidence": r.evidence_refs}


def a_architecture(ctx):
    r = ctx.engine.f.agent("architecture").run(ctx, {"kind": "architecture.propose", "spec": _spec(ctx)})
    if r.status != "success":
        raise StepBlocked(r.summary)
    return {"threats": r.payload["threats"], "decision_proposals": r.payload["decision_proposals"]}


def a_plan(ctx):
    r = ctx.engine.f.agent("planning").run(ctx, {"kind": "planning.plan", "spec": _spec(ctx)})
    if r.status != "success":
        raise StepBlocked(r.summary)
    return {"tasks": r.payload["tasks"]}


def a_baseline_request(ctx):
    spec = _spec(ctx)
    return {"binding": ctx.engine.f.specs.baseline_binding(spec["spec_id"], spec["version"]),
            "roles": baseline_roles(spec["risk_tier"])}


def a_baseline(ctx):
    f = ctx.engine.f
    spec = _spec(ctx)
    ids = ctx.outputs["approve_baseline"]["approvals"]
    res = f.specs.baseline(spec["spec_id"], spec["version"], ids, ctx.actor)
    for t in ctx.outputs["plan"]["tasks"]:
        tn = f.trace.node("TASK", f"{spec['spec_id']}@v{spec['version']}/{t['id']}", digest_obj(t), ctx.project, t)
        for rid in t["requirements"]:
            rn = f.trace.current("REQ", rid, ctx.project)
            if rn:
                f.trace.edge(tn, rn["id"], "implements")
    linked = f.ontology.link_spec(spec)
    return {"spec_node": res["spec_node"], "stale": res["stale"], "concepts": linked}


SPEC_PIPELINE = Workflow("spec_pipeline", "1.0.0", [
    Step("validate", a_validate),
    Step("analyze", a_analyze, ["validate"]),
    Step("architecture", a_architecture, ["analyze"]),
    Step("plan", a_plan, ["architecture"]),
    Step("approve_baseline", kind="approval", approval=a_baseline_request, deps=["plan"]),
    Step("baseline", a_baseline, ["approve_baseline"]),
])


# ======================= Stage 3 — implement in isolation =======================
def d_sandbox(ctx):
    f = ctx.engine.f
    spec = _spec(ctx)
    base = f.specs.current_baseline(spec["spec_id"])
    if not base or base["version"] != spec["version"]:
        raise StepBlocked("delivery must run against the current approved baseline (pinned)")
    sb = f.sandboxes.create(ctx.project, ctx.run_id, f.project(ctx.project)["risk_tier"])
    f.repo.clone_to(ctx.project, sb.work)
    return {"sandbox_id": sb.id, "workdir": str(sb.work), "base": f.repo.head(ctx.project),
            "baseline_digest": base["digest"]}


def _sb(ctx):
    f = ctx.engine.f
    sid = ctx.outputs["sandbox"]["sandbox_id"]
    if sid not in f.sandboxes.active:  # worker restarted: sandbox state is ephemeral -> rebuild deterministically
        raise StepBlocked("sandbox lost after restart; re-run delivery from a fresh sandbox")
    return f.sandboxes.active[sid]


def d_implement(ctx):
    f = ctx.engine.f
    spec, sb = _spec(ctx), _sb(ctx)
    workspace = {p.relative_to(sb.work).as_posix(): p.read_text(errors="ignore")
                 for p in sb.work.rglob("*") if p.is_file() and ".git" not in p.parts and p.stat().st_size < 200_000}
    task = {"kind": "implementation.generate", "spec": spec, "contract": spec["contracts"][0], "workspace": workspace}
    if ctx.input.get("defect"):
        task["defect"] = ctx.input["defect"]
    r = f.agent("implementation").run(ctx, task, env="sandbox")
    if r.status != "success":
        raise StepBlocked(r.summary)
    scope_roots = ("app/", "openapi.json", "tests/unit/")
    for path, content in r.payload["files"].items():
        if not path.startswith(scope_roots) or ".." in path:
            raise StepBlocked(f"implementation attempted to write outside its scoped workspace: {path}")
        p = sb.work / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    denied = [t for t in r.tool_results if not t["ok"]]
    return {"files": sorted(r.payload["files"]), "denied_tool_requests": denied, "evidence": r.evidence_refs}


def d_tests(ctx):
    f = ctx.engine.f
    spec, sb = _spec(ctx), _sb(ctx)
    r = f.agent("test").run(ctx, {"kind": "test.generate", "spec": spec})
    for path, content in r.payload["files"].items():
        if not path.startswith("tests/unit/"):
            raise StepBlocked(f"test agent may only propose unit test candidates, not {path}")
        (sb.work / path).parent.mkdir(parents=True, exist_ok=True)
        (sb.work / path).write_text(content)
    return {"files": sorted(r.payload["files"])}


def _agent_ctx(ctx, role: str) -> CallContext:
    f = ctx.engine.f
    return CallContext(f.directory.get(f"agent:{role}"), ctx.tenant, ctx.project, ctx.run_id, "sandbox",
                       on_call=ctx.charge_tool)


def d_run_tests(ctx):
    f = ctx.engine.f
    res = f.tools.invoke(_agent_ctx(ctx, "test"), "sandbox", "run_tests",
                         {"sandbox_id": ctx.outputs["sandbox"]["sandbox_id"]}, op_id=ctx.op_id("run_tests"))
    if not res["passed"]:
        raise StepBlocked(f"unit tests failing in sandbox: {res['summary']}")
    return res


def d_push_pr(ctx):
    f = ctx.engine.f
    spec = _spec(ctx)
    branch = f"agent/{spec['spec_id'].lower()}-v{spec['version']}-{ctx.run_id[-8:]}"
    c = _agent_ctx(ctx, "implementation")
    sha = f.tools.invoke(c, "repo", "push", {"branch": branch, "workdir": ctx.outputs["sandbox"]["workdir"],
                                             "message": f"feat: implement {spec['spec_id']}@v{spec['version']}"},
                         op_id=ctx.op_id("push"))["sha"]
    pr = f.tools.invoke(c, "repo", "open_pr", {"branch": branch, "title": f"{spec['title']} v{spec['version']}"},
                        op_id=ctx.op_id("open_pr"))
    if pr["head_sha"] != sha:
        raise StepBlocked("pushed head differs from PR head")
    t = f.trace
    cn = t.node("CHANGE", pr["pr_id"], digest_obj(sha), ctx.project, {"head": sha})
    for tn in f.db.all("SELECT id FROM trace_nodes WHERE kind='TASK' AND ref LIKE ? AND status='current'",
                       (f"{spec['spec_id']}@v{spec['version']}/%",)):
        t.edge(cn, tn["id"], "implements_task")
    return {"pr_id": pr["pr_id"], "head_sha": sha, "branch": branch, "change_node": cn}


def d_review(ctx):
    f = ctx.engine.f
    pr = f.repo.pr(ctx.outputs["push_pr"]["pr_id"])
    files = f.repo.diff(ctx.project, pr["base"], pr["head_sha"])
    r = f.agent("review").run(ctx, {"kind": "review.diff", "files": files, "head_sha": pr["head_sha"]})
    for fi in r.payload["findings"]:
        f.tools.invoke(_agent_ctx(ctx, "review"), "review", "create_item",
                       {"kind": "review_finding", "summary": f"[{fi['severity']}] {fi['file']}: {fi['finding']}",
                        "ref": pr["id"]}, op_id=ctx.op_id(f"finding-{digest_obj(fi)[-10:]}"))
    f.sandboxes.active.pop(ctx.outputs["sandbox"]["sandbox_id"]).destroy()
    return {"findings": r.payload["findings"], "advisory": True}


DELIVERY = Workflow("delivery", "1.0.0", [
    Step("sandbox", d_sandbox),
    Step("implement", d_implement, ["sandbox"]),
    Step("tests", d_tests, ["implement"]),
    Step("run_tests", d_run_tests, ["tests"]),
    Step("push_pr", d_push_pr, ["run_tests"]),
    Step("review", d_review, ["push_pr"]),
])


# ======================= Stages 4-6 — validate, package, promote =======================
def r_review_request(ctx):
    pr = ctx.engine.f.repo.pr(ctx.input["pr_id"])
    return {"binding": ApprovalService.binding("pr.merge", pr["head_sha"], "repo", ctx.project), "roles": ["engineer"]}


def r_ci_head(ctx):
    f = ctx.engine.f
    pr = f.repo.pr(ctx.input["pr_id"])
    b = f.ci.run(ctx.project, pr["head_sha"], _spec(ctx), base=pr["base"])
    if b["status"] != "passed":
        raise StepBlocked(f"trusted CI failed on {pr['head_sha'][:12]}: "
                          f"{ {k: v for k, v in b['report']['checks'].items() if v != 'passed'} } {b['report']['details']}")
    return {"build": b["id"]}


def r_merge(ctx):
    f = ctx.engine.f
    pid = ctx.input["pr_id"]
    approvals = ctx.outputs["approve_review"]["approvals"]
    bld = ctx.outputs["ci_head"]["build"]

    def check_review(pr):
        f.approvals.require(approvals, ApprovalService.binding("pr.merge", pr["head_sha"], "repo", pr["project"]), ["engineer"])

    def check_ci(pr):
        b = f.ci.build(bld)
        if b["status"] != "passed" or b["source_sha"] != pr["head_sha"]:
            raise PermissionError("no green trusted CI for exact head revision")
    pr = f.repo.pr(pid)
    if pr["status"] == "merged":
        return {"merged_sha": pr["merged_sha"]}
    return {"merged_sha": f.repo.merge(pid, check_review, check_ci)}


def r_ci_merged(ctx):
    f = ctx.engine.f
    pr = f.repo.pr(ctx.input["pr_id"])
    cn = f.db.one("SELECT id FROM trace_nodes WHERE kind='CHANGE' AND ref=?", (pr["id"],))
    b = f.ci.run(ctx.project, ctx.outputs["merge"]["merged_sha"], _spec(ctx), base=pr["base"],
                 change_ref=cn["id"] if cn else None)
    if b["status"] != "passed":
        raise StepBlocked(f"trusted CI failed on merge commit: {b['report']['checks']}")
    return {"build": b["id"], "artifact": b["artifact_digest"]}


def r_package(ctx):
    f = ctx.engine.f
    spec = _spec(ctx)
    rel = f.release.create(ctx.project, ctx.outputs["ci_merged"]["build"], spec["spec_id"], ctx.input["pr_id"], ctx.actor)
    adv = f.agent("release-advisor").run(ctx, {"kind": "release.advise", "evidence": f.release.evidence_summary(rel["id"])})
    return {"release_id": rel["id"], "manifest_digest": rel["manifest_digest"], "advice": adv.payload["recommendation"]}


def r_release_request(ctx):
    f = ctx.engine.f
    rid = ctx.outputs["package"]["release_id"]
    return {"binding": f.release.binding(rid, ctx.input.get("env", "nonprod")), "roles": ["release_owner"]}


def r_promote(ctx):
    f = ctx.engine.f
    rid = ctx.outputs["package"]["release_id"]
    ids = ctx.outputs["approve_release"]["approvals"]
    for a in ids:
        f.release.link_approval(rid, a)
    return f.release.promote(rid, ctx.input.get("env", "nonprod"), ids, ctx.actor)


RELEASE = Workflow("release", "1.0.0", [
    Step("approve_review", kind="approval", approval=r_review_request),
    Step("ci_head", r_ci_head, ["approve_review"], max_attempts=1),
    Step("merge", r_merge, ["ci_head"]),
    Step("ci_merged", r_ci_merged, ["merge"], max_attempts=1),
    Step("package", r_package, ["ci_merged"]),
    Step("approve_release", kind="approval", approval=r_release_request, deps=["package"]),
    Step("promote", r_promote, ["approve_release"], max_attempts=1),
])


# ======================= Ontology discovery =======================
def o_discover(ctx):
    from .ontology.discovery import discover
    f = ctx.engine.f
    spec = _spec(ctx)
    docs = ctx.input.get("documents", [])
    return discover(f, spec, docs, run_ctx=ctx)


ONTOLOGY_DISCOVERY = Workflow("ontology_discovery", "1.0.0", [Step("discover", o_discover, max_attempts=1)])


def register_all(f) -> None:
    for wf in (SPEC_PIPELINE, DELIVERY, RELEASE, ONTOLOGY_DISCOVERY):
        f.engine.register(wf)
