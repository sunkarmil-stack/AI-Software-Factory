"""Ontology management API (mounted by factory.api.create_app).

Reads: any authenticated principal. Proposals: humans or agents (as themselves).
Approvals/rejections: humans holding a role the proposal requires (enforced by the registry
and the approval service, not by this layer).
"""
from __future__ import annotations

from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

from ..core.approvals import ApprovalError
from . import rules as rule_engine
from .actions import ActionDenied
from .registry import OntologyError

UI = Path(__file__).with_name("ui.html")


def routes(f, guard) -> list[Route]:
    o = f.ontology

    def err(e, code=400):
        return JSONResponse({"error": str(e)}, code)

    async def whoami(req, p):
        return JSONResponse({"id": p.id, "kind": p.kind, "roles": sorted(p.roles)})

    async def concepts(req, p):
        return JSONResponse(o.concepts(include_deprecated=req.query_params.get("all") == "1"))

    async def concept(req, p):
        name = req.path_params["name"]
        c = o.concept(name)
        if not c:
            return err("not found", 404)
        try:
            impact = o.impact(name)
        except OntologyError:
            impact = {"dependents": {}, "related_concepts": []}
        return JSONResponse({"concept": c, "history": o.history(name), "impact": impact,
                             "proposals": [x for x in o.proposals() if x["target"] == name]})

    async def graph(req, p):
        return JSONResponse(o.graph())

    async def proposals(req, p):
        return JSONResponse(o.proposals(req.query_params.get("status") or None))

    async def propose(req, p):
        b = await req.json()
        try:
            r = o.propose(b["kind"], b.get("target"), b.get("payload", {}), b.get("provenance", {}), p.id)
        except (KeyError, OntologyError) as e:
            return err(e)
        return JSONResponse(r, 201 if r["status"] != "invalid" else 422)

    async def approve(req, p):
        b = await req.json()
        try:
            return JSONResponse(o.approve(req.path_params["pid"], p.id, b["role"], b.get("rationale", "")))
        except ApprovalError as e:
            return err(e, 403)
        except OntologyError as e:
            return err(e, 409)

    async def reject(req, p):
        b = await req.json()
        try:
            return JSONResponse(o.reject(req.path_params["pid"], p.id, b.get("reason", "")))
        except OntologyError as e:
            return err(e, 403)

    async def specs(req, p):
        return JSONResponse(f.db.all("SELECT spec_id, version, project, status FROM specs ORDER BY spec_id, version"))

    async def discover(req, p):
        b = await req.json()
        s = f.specs.get(b["spec_id"], int(b["version"])) if b.get("spec_id") else None
        if not s:
            return err("unknown spec", 404)
        project = s["project"]
        if project not in {r["id"] for r in f.db.all("SELECT id FROM projects")}:
            return err("spec project is not onboarded", 404)
        rid = f.engine.start("ontology_discovery", project, f.project(project)["tenant"], p.id,
                             {"spec_id": b["spec_id"], "version": int(b["version"]), "documents": b.get("documents", [])})
        state = f.engine.run(rid)
        run = f.engine.get(rid)
        return JSONResponse({"run_id": rid, "state": state, "error": run["error"],
                             "result": run["steps"]["discover"]["output"]})

    async def check(req, p):
        b = await req.json()
        spec = b.get("spec") or (f.specs.get(b["spec_id"], int(b["version"])) or {}).get("body")
        if not spec:
            return err("spec not found", 404)
        return JSONResponse(o.check_spec(spec))

    async def export(req, p):
        return PlainTextResponse(o.export_turtle(), media_type="text/turtle")

    async def actions(req, p):
        return JSONResponse(list(o.all_actions().values()))

    async def execute(req, p):
        b = await req.json()
        try:
            return JSONResponse(f.actions.execute(req.path_params["name"], p.id, b.get("subject", {}), b.get("params", {}),
                                                  b.get("env", "nonprod")))
        except ActionDenied as e:
            return err(e, 403)

    async def action_requests(req, p):
        return JSONResponse(f.actions.requests(req.query_params.get("status") or None))

    async def approve_request(req, p):
        b = await req.json()
        try:
            return JSONResponse(f.actions.approve(req.path_params["rid"], p.id, b["role"]))
        except ApprovalError as e:
            return err(e, 403)
        except ActionDenied as e:
            return err(e, 409)

    async def reject_request(req, p):
        b = await req.json()
        try:
            return JSONResponse(f.actions.reject(req.path_params["rid"], p.id, b.get("reason", "")))
        except ActionDenied as e:
            return err(e, 403)

    async def test_rule(req, p):
        b = await req.json()
        try:
            return JSONResponse({"result": rule_engine.evaluate(b["expression"], b.get("data", {})),
                                 "names": sorted(rule_engine.names(b["expression"]))})
        except rule_engine.UnknownValue as e:
            return JSONResponse({"result": "unknown", "missing": e.args[0]})
        except rule_engine.RuleSyntaxError as e:
            return err(e)

    async def ui(req):
        return HTMLResponse(UI.read_text())

    return [
        Route("/ontology", ui),
        Route("/v1/whoami", guard(whoami)),
        Route("/v1/ontology/concepts", guard(concepts)),
        Route("/v1/ontology/concepts/{name}", guard(concept)),
        Route("/v1/ontology/graph", guard(graph)),
        Route("/v1/ontology/proposals", guard(proposals)),
        Route("/v1/ontology/proposals/new", guard(propose), methods=["POST"]),
        Route("/v1/ontology/proposals/{pid}/approve", guard(approve), methods=["POST"]),
        Route("/v1/ontology/proposals/{pid}/reject", guard(reject), methods=["POST"]),
        Route("/v1/ontology/specs", guard(specs)),
        Route("/v1/ontology/discover", guard(discover), methods=["POST"]),
        Route("/v1/ontology/check-spec", guard(check), methods=["POST"]),
        Route("/v1/ontology/export.ttl", guard(export)),
        Route("/v1/ontology/actions", guard(actions)),
        Route("/v1/ontology/actions/{name}/execute", guard(execute), methods=["POST"]),
        Route("/v1/ontology/action-requests", guard(action_requests)),
        Route("/v1/ontology/action-requests/{rid}/approve", guard(approve_request), methods=["POST"]),
        Route("/v1/ontology/action-requests/{rid}/reject", guard(reject_request), methods=["POST"]),
        Route("/v1/ontology/rules/test", guard(test_rule), methods=["POST"]),
    ]
