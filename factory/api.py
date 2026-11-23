"""Factory control-plane API (Starlette; FastAPI-compatible routing model).

Submission endpoints return a durable run id immediately (NFR 02) and execute work on a
bounded worker pool; when admission capacity is reached they reject explicitly with 429
(NFR 04). AuthN here uses dev bearer tokens from catalog/identities.yaml; in the Azure
reference deployment, Entra ID tokens are validated at the API gateway and the backend
still enforces resource authorization per request.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from .core.approvals import ApprovalError
from .factory import Factory
from .ops import audit, dashboard

MAX_QUEUE = 50


class Admission:
    def __init__(self, workers: int = 4, max_queue: int = MAX_QUEUE):
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.sem = threading.BoundedSemaphore(max_queue)

    def submit(self, fn, *a) -> bool:
        if not self.sem.acquire(blocking=False):
            return False
        def run():
            try:
                fn(*a)
            finally:
                self.sem.release()
        self.pool.submit(run)
        return True


def create_app(f: Factory | None = None) -> Starlette:
    f = f or Factory()
    adm = Admission()

    def who(req: Request):
        tok = (req.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        p = f.directory.by_token(tok) if tok else None
        if not p:
            raise PermissionError("unauthenticated")
        return p

    def guard(handler, role: str | None = None):
        async def inner(req: Request):
            try:
                p = who(req)
                if role and not p.has_role(role):
                    return JSONResponse({"error": f"requires role {role}"}, 403)
                return await handler(req, p)
            except PermissionError as e:
                return JSONResponse({"error": str(e)}, 401 if "unauthenticated" in str(e) else 403)
            except (KeyError, ValueError) as e:
                return JSONResponse({"error": str(e)}, 400)
        return inner

    async def health(req):
        ok, n = f.ledger.verify_chain()
        return JSONResponse({"status": "ok" if ok else "degraded", "evidence_records": n})

    async def onboard(req, p):
        b = await req.json()
        return JSONResponse(f.onboard(b["project"], b["tenant"], b["owners"], int(b["risk_tier"]), float(b["budget_usd"]),
                                      actor=p.id), 201)

    async def start_run(req, p):
        b = await req.json()
        if b["workflow"] == "spec_pipeline" and not p.has_role("product_owner"):
            return JSONResponse({"error": "only a product owner submits specifications"}, 403)
        if b["project"] not in {r["id"] for r in f.db.all("SELECT id FROM projects")}:
            return JSONResponse({"error": "unknown project"}, 404)
        tenant = f.project(b["project"])["tenant"]
        if not p.in_tenant(tenant):
            return JSONResponse({"error": "cross-tenant submission denied"}, 403)
        inp = b.get("input", {})
        if b["workflow"] == "spec_pipeline":
            inp = {"spec": inp["spec"], "spec_id": inp["spec"]["spec_id"], "version": inp["spec"]["version"]}
        rid = f.engine.start(b["workflow"], b["project"], tenant, p.id, inp)
        if not adm.submit(f.engine.run, rid):
            f.engine.cancel(rid, "admission", "rejected: admission capacity reached")
            return JSONResponse({"error": "admission capacity reached; retry later", "run_id": rid}, 429)
        return JSONResponse({"run_id": rid, "status": "accepted"}, 202)

    async def get_run(req, p):
        r = f.engine.get(req.path_params["rid"])
        if not p.in_tenant(r["tenant"]):
            return JSONResponse({"error": "not found"}, 404)
        return JSONResponse(r)

    async def approve(req, p):
        rid = req.path_params["rid"]
        b = await req.json()
        r = f.engine.get(rid)
        st = r["steps"].get(b["step_id"])
        if not st or st["state"] != "WAITING_APPROVAL":
            return JSONResponse({"error": "step is not awaiting approval"}, 409)
        binding = st["output"]["request"]["binding"]
        try:
            aid = f.approvals.approve(p.id, b["role"], binding, rationale=b.get("rationale", ""))
        except ApprovalError as e:
            return JSONResponse({"error": str(e)}, 403)
        existing = r["input"].get("approvals", {}).get(b["step_id"], [])
        f.engine.provide_approvals(rid, b["step_id"], existing + [aid])
        adm.submit(f.engine.run, rid)
        return JSONResponse({"approval_id": aid, "binding": binding}, 201)

    async def cancel(req, p):
        return JSONResponse({"cancelled": f.engine.cancel(req.path_params["rid"], p.id)})

    async def control(req, p):
        b = await req.json()
        cid = f.controls.activate(b["kind"], b["target"], p.id, b.get("reason", ""))
        f.controls.force_refresh()
        if b["kind"] == "cancel_run":
            f.engine.cancel(b["target"], p.id, "emergency control")
        return JSONResponse({"control_id": cid}, 201)

    async def release_audit(req, p):
        if not (p.has_role("auditor") or p.has_role("release_owner")):
            return JSONResponse({"error": "requires auditor"}, 403)
        rec = audit.reconstruct(f, req.path_params["rel"])
        rec.pop("chain")
        return JSONResponse(rec)

    async def export(req, p):
        return JSONResponse(audit.export(f, req.query_params["project"]))

    async def reviews(req, p):
        return JSONResponse(f.db.all("SELECT id, kind, ref, summary, status FROM review_items WHERE project=?",
                                     (req.query_params.get("project", ""),)))

    async def metrics(req, p):
        return JSONResponse(dashboard.snapshot(f))

    async def dash(req):
        return HTMLResponse(dashboard.render(f))

    routes = [
        Route("/healthz", health),
        Route("/dashboard", dash),
        Route("/v1/projects", guard(onboard, "platform_admin"), methods=["POST"]),
        Route("/v1/runs", guard(start_run), methods=["POST"]),
        Route("/v1/runs/{rid}", guard(get_run)),
        Route("/v1/runs/{rid}/approvals", guard(approve), methods=["POST"]),
        Route("/v1/runs/{rid}/cancel", guard(cancel, "operator"), methods=["POST"]),
        Route("/v1/ops/controls", guard(control, "operator"), methods=["POST"]),
        Route("/v1/releases/{rel}/audit", guard(release_audit)),
        Route("/v1/audit/export", guard(export, "auditor")),
        Route("/v1/review-items", guard(reviews)),
        Route("/v1/metrics", guard(metrics)),
    ]
    from .ontology.api import routes as ontology_routes
    routes += ontology_routes(f, guard)
    app = Starlette(routes=routes)
    app.state.factory = f
    return app
