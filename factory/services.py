"""Tool implementations bound to factory services, plus a synthetic authoritative
downstream (payments ledger) used to prove idempotency and reconciliation (REQ 022, ACC 04)."""
from __future__ import annotations

import threading
from pathlib import Path

from .core.db import DB, j, unj
from .core.util import new_id, now
from .orchestrator.engine import SimulatedCrash
from .tools.gateway import CallContext, OutcomeUnknown, PermanentError, ToolImpl, TransientError


class PaymentsLedger:
    """Authoritative system of record with its own storage, queryable by operation id."""

    def __init__(self, path: Path):
        self.db = DB(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS entries(ledger_id TEXT PRIMARY KEY, op_id TEXT UNIQUE, account TEXT,"
                        " amount INTEGER, ts REAL)")
        self.crash_after_commit = False
        self.lose_response = False
        self.unavailable = False
        self.lock = threading.Lock()

    def write(self, op_id: str, account: str, amount: int) -> dict:
        if self.unavailable:
            raise TransientError("payments ledger 503")
        with self.lock:
            existing = self.db.one("SELECT * FROM entries WHERE op_id=?", (op_id,))
            if existing:  # downstream also dedupes on op id (defence in depth)
                return {"ledger_id": existing["ledger_id"], "status": "committed"}
            lid = new_id("LED")
            self.db.execute("INSERT INTO entries(ledger_id,op_id,account,amount,ts) VALUES(?,?,?,?,?)",
                            (lid, op_id, account, amount, now()))
        if self.crash_after_commit:
            self.crash_after_commit = False
            raise SimulatedCrash("worker killed after downstream commit")
        if self.lose_response:
            self.lose_response = False
            raise OutcomeUnknown("connection reset after request sent")
        return {"ledger_id": lid, "status": "committed"}

    def by_op(self, op_id: str) -> dict | None:
        r = self.db.one("SELECT * FROM entries WHERE op_id=?", (op_id,))
        return {"ledger_id": r["ledger_id"], "status": "committed"} if r else None

    def count(self, account: str | None = None) -> int:
        if account:
            return self.db.one("SELECT COUNT(*) n FROM entries WHERE account=?", (account,))["n"]
        return self.db.one("SELECT COUNT(*) n FROM entries")["n"]


def build_impls(f) -> dict[str, ToolImpl]:
    def repo(action, args, ctx: CallContext):
        if action == "push":
            return {"sha": f.repo.push_branch(ctx.project, Path(args["workdir"]), args["branch"], ctx.principal.id,
                                              args.get("message", f"agent change ({ctx.run_id})"))}
        if action == "open_pr":
            return f.repo.open_pr(ctx.project, args["branch"], args["title"], ctx.principal.id, {"run": ctx.run_id})
        raise PermanentError(action)

    def sandbox(action, args, ctx):
        sb = f.sandboxes.active[args["sandbox_id"]]
        r = sb.pytest(args.get("paths", ["tests/unit"]))
        return {"passed": r.code == 0, "summary": (r.stdout.strip().splitlines() or ["no output"])[-1]}

    def review(action, args, ctx):
        rid = new_id("RVW")
        f.db.execute("INSERT INTO review_items(id,project,kind,ref,summary,status,data,created) VALUES(?,?,?,?,?,?,?,?)",
                     (rid, ctx.project, args["kind"], args.get("ref", ""), args["summary"], "open",
                      j({"by": ctx.principal.id, "run": ctx.run_id}), now()))
        return {"id": rid}

    def deploy(action, args, ctx):
        if action == "request":
            rid = new_id("DRQ")
            f.db.execute("INSERT INTO review_items(id,project,kind,ref,summary,status,data,created) VALUES(?,?,?,?,?,?,?,?)",
                         (rid, ctx.project, "deploy_request", args["release_id"], f"deploy to {args['env']}", "open",
                          j({"by": ctx.principal.id}), now()))
            return {"request_id": rid, "status": "pending_human_authorization"}
        raise PermanentError("deploy.execute is performed only by the promotion service")

    def secrets(action, args, ctx):
        return {"value": f.secret_store.get(args["name"], "")}

    def kb(action, args, ctx):
        from .context.retrieval import Requester
        rq = Requester(ctx.tenant, ctx.project, args.get("user", ctx.principal.id), args.get("groups", []), "product.rag")
        return {"passages": f.retrieval.search(rq, args["query"], args.get("k", 4))}

    def review_queue(action, args, ctx):
        rid = new_id("HRQ")
        f.db.execute("INSERT INTO review_items(id,project,kind,ref,summary,status,data,created) VALUES(?,?,?,?,?,?,?,?)",
                     (rid, ctx.project, "rag_review", args["question"][:80], args.get("reason", "low confidence"),
                      "open", j({"draft": args["draft"]}), now()))
        return {"id": rid}

    def payments(action, args, ctx):
        return f.payments.write(ctx.op_id, args["account"], args["amount_cents"])

    def prod_db(action, args, ctx):
        return {"rows": []}

    return {"repo": ToolImpl(repo), "sandbox": ToolImpl(sandbox), "review": ToolImpl(review), "deploy": ToolImpl(deploy),
            "secrets": ToolImpl(secrets), "kb": ToolImpl(kb), "review_queue": ToolImpl(review_queue),
            "payments": ToolImpl(payments, reconcile=lambda op: f.payments.by_op(op)), "prod_db": ToolImpl(prod_db)}
