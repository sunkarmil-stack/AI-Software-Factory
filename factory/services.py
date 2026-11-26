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


    def holds(action, args, ctx):
        """Synthetic system of record for account holds (stands in for the core deposits platform)."""
        existing = f.db.one("SELECT hold_id FROM holds_sor WHERE op_id=?", (ctx.op_id,))
        if existing:
            return {"hold_id": existing["hold_id"], "status": "placed"}
        hid = new_id("HLD")
        f.db.execute("INSERT INTO holds_sor(hold_id,op_id,tenant,account_id,amount_cents,reason,status,created)"
                     " VALUES(?,?,?,?,?,?,?,?)", (hid, ctx.op_id, args["tenant"], args["subject_id"], args["amount_cents"],
                                                  args["reason"], "placed", now()))
        return {"hold_id": hid, "status": "placed"}

    def holds_reconcile(op_id):
        r = f.db.one("SELECT hold_id FROM holds_sor WHERE op_id=?", (op_id,))
        return {"hold_id": r["hold_id"], "status": "placed"} if r else None

    # ---- wealth-advisory systems of record (synthetic book; production reaches them via the AI gateway)
    import yaml as _yaml
    from .core.util import REPO_ROOT
    book = _yaml.safe_load((REPO_ROOT / "samples" / "wealth" / "book.yaml").read_text())

    def _account(args, ctx):
        if ctx.tenant != "wealth" or args["account_id"] not in book["accounts"]:
            raise PermanentError("account not found")      # no cross-tenant existence leak
        return book["accounts"][args["account_id"]]

    def portfolio(action, args, ctx):
        a = _account(args, ctx)
        return {"account_id": args["account_id"], "client_id": a["client_id"], "account_type": a["account_type"],
                "holdings": dict(a["holdings"]), "cash_cents": a["cash_cents"], "ips": a["ips"]}

    def market_data(action, args, ctx):
        codes = args["codes"]
        missing = [c for c in codes if c not in book["prices_cents"]]
        if missing:
            raise PermanentError(f"no quote for {missing}")
        return {"prices_cents": {c: book["prices_cents"][c] for c in codes}, "as_of": book["as_of"]}

    def kyc(action, args, ctx):
        _account(args, ctx)
        return dict(book["kyc"][args["account_id"]])

    def product_shelf(action, args, ctx):
        return {"products": {c: book["products"][c] for c in args["codes"] if c in book["products"]}}

    def _sor(table, idcol, prefix, cols, args, ctx):
        r = f.db.one(f"SELECT {idcol} FROM {table} WHERE op_id=?", (ctx.op_id,))
        if r:
            return r[idcol]
        rid = new_id(prefix)
        names = [idcol, "op_id", "tenant", "account_id", *cols, "created"]
        vals = [rid, ctx.op_id, args["tenant"], args["subject_id"], *[args[c] for c in cols], now()]
        f.db.execute(f"INSERT INTO {table}({','.join(names)}) VALUES({','.join('?' * len(names))})", vals)
        return rid

    def advice_records(action, args, ctx):
        rid = _sor("advice_records_sor", "recommendation_id", "REC",
                   ["product_code", "side", "amount_cents", "rationale", "snapshot_id"], args, ctx)
        return {"recommendation_id": rid, "status": "recorded"}

    def advice_reconcile(op_id):
        r = f.db.one("SELECT recommendation_id FROM advice_records_sor WHERE op_id=?", (op_id,))
        return {"recommendation_id": r["recommendation_id"], "status": "recorded"} if r else None

    def orders(action, args, ctx):
        rec = f.db.one("SELECT * FROM advice_records_sor WHERE recommendation_id=?", (args["recommendation_id"],))
        if not rec or rec["account_id"] != args["subject_id"] or \
                (rec["product_code"], rec["side"], rec["amount_cents"]) != (args["product_code"], args["side"], args["amount_cents"]):
            raise PermanentError("order does not match a recorded recommendation")   # defence in depth at the SoR
        dup = f.db.one("SELECT order_id, op_id FROM orders_sor WHERE recommendation_id=?", (args["recommendation_id"],))
        if dup and dup["op_id"] != ctx.op_id:
            raise PermanentError(f"recommendation already implemented by {dup['order_id']}")
        args = {**args, "status": "accepted"}
        oid = _sor("orders_sor", "order_id", "ORD", ["recommendation_id", "product_code", "side", "amount_cents", "status"],
                   args, ctx)
        return {"order_id": oid, "status": "accepted"}

    def orders_reconcile(op_id):
        r = f.db.one("SELECT order_id FROM orders_sor WHERE op_id=?", (op_id,))
        return {"order_id": r["order_id"], "status": "accepted"} if r else None

    # ---- regulatory reporting systems of record
    def return_registry(action, args, ctx):
        r = f.db.one("SELECT certification_id FROM return_registry_sor WHERE op_id=?", (ctx.op_id,))
        if r:
            return {"certification_id": r["certification_id"], "status": "certified"}
        cid = new_id("CERT")
        f.db.execute("INSERT INTO return_registry_sor VALUES(?,?,?,?,?,?,?,?,?)",
                     (cid, ctx.op_id, args["tenant"], args["subject_id"], args["return_code"], args["period_end"],
                      args["package_digest"], args["snapshot_id"], now()))
        return {"certification_id": cid, "status": "certified"}

    def registry_reconcile(op_id):
        r = f.db.one("SELECT certification_id FROM return_registry_sor WHERE op_id=?", (op_id,))
        return {"certification_id": r["certification_id"], "status": "certified"} if r else None

    def regulatory_filing(action, args, ctx):
        cert = f.db.one("SELECT * FROM return_registry_sor WHERE certification_id=?", (args["certification_id"],))
        if not cert or (cert["return_code"], cert["period_end"], cert["package_digest"]) != \
                (args["return_code"], args["period_end"], args["package_digest"]):
            raise PermanentError("filing does not match a certified return package")
        r = f.db.one("SELECT filing_id, receipt FROM filings_sor WHERE op_id=?", (ctx.op_id,))
        if r:
            return {"filing_id": r["filing_id"], "receipt": r["receipt"], "status": "accepted"}
        fid, receipt = new_id("FIL"), "RRS-STANDIN-" + new_id("RCPT")[-8:].upper()
        f.db.execute("INSERT INTO filings_sor VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (fid, ctx.op_id, args["tenant"], args["subject_id"], args["certification_id"], args["return_code"],
                      args["period_end"], args["package_digest"], receipt, now()))
        return {"filing_id": fid, "receipt": receipt, "status": "accepted"}

    def filing_reconcile(op_id):
        r = f.db.one("SELECT filing_id, receipt FROM filings_sor WHERE op_id=?", (op_id,))
        return {"filing_id": r["filing_id"], "receipt": r["receipt"], "status": "accepted"} if r else None

    # ---- retail account risk systems of record (synthetic)
    risk_book = _yaml.safe_load((REPO_ROOT / "samples" / "retail_risk" / "book.yaml").read_text())

    def _risk_account(args, ctx):
        if ctx.tenant != "retail" or args["account_id"] not in risk_book["accounts"]:
            raise PermanentError("account not found")
        return risk_book["accounts"][args["account_id"]]

    def transactions(action, args, ctx):
        a = _risk_account(args, ctx)
        return {"account_id": args["account_id"], "as_of": risk_book["as_of"], "profile": dict(a["profile"]),
                "transactions": [dict(t) for t in a["transactions"]], "vulnerable_customer": a["vulnerable_customer"],
                "customer_id": a["customer_id"], "product_type": a["product_type"]}

    def credit_exposure(action, args, ctx):
        a = _risk_account(args, ctx)
        return {"account_id": args["account_id"], "credit": dict(a["credit"])}

    def _risk_sor(table, idcol, prefix, cols, status, args, ctx):
        r = f.db.one(f"SELECT {idcol} FROM {table} WHERE op_id=?", (ctx.op_id,))
        if r:
            return r[idcol]
        rid = new_id(prefix)
        names = [idcol, "op_id", "tenant", "account_id", *cols, "status", "created"]
        f.db.execute(f"INSERT INTO {table}({','.join(names)}) VALUES({','.join('?' * len(names))})",
                     [rid, ctx.op_id, args["tenant"], args["subject_id"], *[args[c] for c in cols], status, now()])
        return rid

    def case_management(action, args, ctx):
        cid = _risk_sor("risk_cases_sor", "case_id", "CASE", ["case_type", "alert_id", "score", "reason_codes"], "open", args, ctx)
        return {"case_id": cid, "status": "open"}

    def account_controls(action, args, ctx):
        case = f.db.one("SELECT * FROM risk_cases_sor WHERE case_id=?", (args["case_id"],))
        if not case or case["account_id"] != args["subject_id"] or case["case_type"] != "fraud":
            raise PermanentError("restriction does not match an open fraud case on this account")
        rid = _risk_sor("restrictions_sor", "restriction_id", "RSTR", ["case_id", "restriction", "duration_hours"],
                        "active", args, ctx)
        return {"restriction_id": rid, "status": "active", "expires_at": f"+{args['duration_hours']}h"}

    def credit_limits(action, args, ctx):
        case = f.db.one("SELECT * FROM risk_cases_sor WHERE case_id=?", (args["case_id"],))
        if not case or case["account_id"] != args["subject_id"] or case["case_type"] != "credit":
            raise PermanentError("limit change does not match a credit case on this account")
        rid = _risk_sor("limit_changes_sor", "change_id", "LIMC",
                        ["case_id", "current_limit_cents", "new_limit_cents", "notice_days"], "scheduled", args, ctx)
        return {"change_id": rid, "status": "scheduled", "effective_date": f"+{args['notice_days']}d"}

    def _risk_reconcile(table, idcol, extra):
        def rec(op_id):
            r = f.db.one(f"SELECT {idcol} FROM {table} WHERE op_id=?", (op_id,))
            return {idcol: r[idcol], **extra} if r else None
        return rec

    return {"transactions": ToolImpl(transactions), "credit_exposure": ToolImpl(credit_exposure),
            "case_management": ToolImpl(case_management, reconcile=_risk_reconcile("risk_cases_sor", "case_id", {"status": "open"})),
            "account_controls": ToolImpl(account_controls, reconcile=_risk_reconcile("restrictions_sor", "restriction_id",
                                                                                      {"status": "active", "expires_at": ""})),
            "credit_limits": ToolImpl(credit_limits, reconcile=_risk_reconcile("limit_changes_sor", "change_id",
                                                                                {"status": "scheduled", "effective_date": ""})),
            "return_registry": ToolImpl(return_registry, reconcile=registry_reconcile),
            "regulatory_filing": ToolImpl(regulatory_filing, reconcile=filing_reconcile),
            "portfolio": ToolImpl(portfolio), "market_data": ToolImpl(market_data), "kyc": ToolImpl(kyc),
            "product_shelf": ToolImpl(product_shelf),
            "advice_records": ToolImpl(advice_records, reconcile=advice_reconcile),
            "orders": ToolImpl(orders, reconcile=orders_reconcile),
            "repo": ToolImpl(repo), "sandbox": ToolImpl(sandbox), "review": ToolImpl(review), "deploy": ToolImpl(deploy),
            "secrets": ToolImpl(secrets), "kb": ToolImpl(kb), "review_queue": ToolImpl(review_queue),
            "payments": ToolImpl(payments, reconcile=lambda op: f.payments.by_op(op)), "prod_db": ToolImpl(prod_db),
            "holds": ToolImpl(holds, reconcile=holds_reconcile)}
