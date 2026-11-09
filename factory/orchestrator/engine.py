"""Durable workflow engine — the single authoritative owner of workflow state (REQ 007-011).

- Run + step state persisted after every transition (SQLite here, Postgres in prod).
- Lease-based workers: a crashed worker's run is resumed by another after lease expiry.
- Completed steps are never re-executed; a re-executed step reuses deterministic
  operation ids so the tool gateway can reconcile side effects (REQ 022, ACC 04).
- Per-run limits on elapsed time, attempts, model tokens, cost, tool calls and fan-out,
  charged to the *root* run so child runs cannot bypass budgets (REQ 009, REQ 032).
- Approval waits are first-class states; cancellation propagates to child runs.
"""
from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.db import DB, j, unj
from ..core.evidence import Ledger
from ..core.telemetry import Telemetry
from ..core.util import new_id, now

TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


class StepBlocked(Exception):
    """Ambiguity, unexpected authority or missing input: stop and escalate (REQ 005)."""


class StepFailed(Exception):
    pass


class LimitExceeded(Exception):
    pass


class SimulatedCrash(BaseException):
    """Test hook: kills the worker mid-step without any cleanup (ACC 04, REQ 008)."""


@dataclass
class Step:
    id: str
    fn: Callable[["RunContext"], dict] | None = None
    deps: list[str] = field(default_factory=list)
    kind: str = "task"                   # task | approval
    approval: Callable[["RunContext"], dict] | None = None  # returns {"binding":..., "roles": [...]}
    max_attempts: int = 2


@dataclass
class Workflow:
    name: str
    version: str
    steps: list[Step]


DEFAULT_LIMITS = {"max_elapsed_s": 3600, "max_attempts": 3, "max_model_tokens": 400_000, "max_cost_usd": 25.0,
                  "max_tool_calls": 500, "max_fanout": 20}
ZERO = {"tool_calls": 0, "model_tokens": 0, "cost_usd": 0.0, "fanout": 0}


class RunContext:
    def __init__(self, engine: "Engine", run: dict, step: Step, outputs: dict):
        self.engine, self.run, self.step = engine, run, step
        self.run_id, self.root_id = run["run_id"], run["root_id"]
        self.project, self.tenant, self.actor = run["project"], run["tenant"], run["actor"]
        self.input = unj(run["input"], {})
        self.outputs = outputs
        self.attempt = 0

    def op_id(self, name: str) -> str:
        """Deterministic across retries and worker restarts."""
        return f"{self.run_id}:{self.step.id}:{name}"

    def charge_tool(self, tool: str = "") -> None:
        self.engine._charge(self.run_id, "tool_calls", 1)

    def charge_model(self, tokens: int, cost: float) -> None:
        self.engine._charge(self.run_id, "model_tokens", tokens)
        self.engine._charge(self.run_id, "cost_usd", cost)

    def spawn(self, workflow: str, input: dict) -> str:
        self.engine._charge(self.run_id, "fanout", 1)
        return self.engine.start(workflow, self.project, self.tenant, self.actor, input,
                                 parent=self.run_id)


class Engine:
    def __init__(self, db: DB, ledger: Ledger, telemetry: Telemetry, worker_id: str | None = None,
                 lease_s: float = 30.0):
        self.db, self.ledger, self.tm = db, ledger, telemetry
        self.worker = worker_id or new_id("WRK")
        self.lease_s = lease_s
        self.workflows: dict[str, Workflow] = {}
        self.approval_resolver: Callable[[list[str], dict, list[str]], list[str]] | None = None

    def register(self, wf: Workflow) -> None:
        self.workflows[wf.name] = wf

    # ---------------- lifecycle ----------------
    def start(self, workflow: str, project: str, tenant: str, actor: str, input: dict,
              limits: dict | None = None, parent: str | None = None) -> str:
        if workflow not in self.workflows:
            raise KeyError(workflow)
        rid = new_id("RUN")
        root = rid
        if parent:
            p = self.db.one("SELECT root_id, limits FROM runs WHERE run_id=?", (parent,))
            root = p["root_id"]
            limits = unj(p["limits"])
        lim = {**DEFAULT_LIMITS, **(limits or {})}
        self.db.execute("INSERT INTO runs(run_id,parent_id,root_id,workflow,project,tenant,actor,state,input,limits,"
                        "counters,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (rid, parent, root, workflow, project, tenant, actor, "PENDING", j(input), j(lim), j(ZERO),
                         now(), now()))
        for s in self.workflows[workflow].steps:
            self.db.execute("INSERT INTO steps(run_id,step_id,state,attempts,output,updated) VALUES(?,?,?,?,?,?)",
                            (rid, s.id, "PENDING", 0, None, now()))
        self.ledger.record("run.started", rid, actor, {"workflow": workflow, "parent": parent, "root": root,
                                                         "version": self.workflows[workflow].version}, project)
        self.tm.metric("factory", "runs_started", 1, workflow=workflow)
        return rid

    def get(self, rid: str) -> dict:
        r = self.db.one("SELECT * FROM runs WHERE run_id=?", (rid,))
        r["steps"] = {s["step_id"]: {"state": s["state"], "attempts": s["attempts"], "output": unj(s["output"])}
                      for s in self.db.all("SELECT * FROM steps WHERE run_id=?", (rid,))}
        r["counters"], r["limits"], r["input"] = unj(r["counters"]), unj(r["limits"]), unj(r["input"])
        r["children"] = [c["run_id"] for c in self.db.all("SELECT run_id FROM runs WHERE parent_id=?", (rid,))]
        return r

    def _set(self, rid: str, state: str, error: str | None = None) -> None:
        self.db.execute("UPDATE runs SET state=?, error=?, updated=? WHERE run_id=?", (state, error, now(), rid))
        self.ledger.record("run.state", rid, self.worker, {"state": state, "error": error})

    def _set_step(self, rid: str, sid: str, state: str, output: Any = None, attempts: int | None = None) -> None:
        if attempts is None:
            self.db.execute("UPDATE steps SET state=?, output=COALESCE(?, output), updated=? WHERE run_id=? AND step_id=?",
                            (state, j(output) if output is not None else None, now(), rid, sid))
        else:
            self.db.execute("UPDATE steps SET state=?, output=COALESCE(?, output), attempts=?, updated=? WHERE run_id=? AND step_id=?",
                            (state, j(output) if output is not None else None, attempts, now(), rid, sid))

    def _charge(self, rid: str, key: str, amount: float) -> None:
        r = self.db.one("SELECT root_id FROM runs WHERE run_id=?", (rid,))
        targets = {rid, r["root_id"]}
        for t in targets:
            with self.db.tx():
                row = self.db.one("SELECT counters, limits FROM runs WHERE run_id=?", (t,))
                c, lim = unj(row["counters"]), unj(row["limits"])
                c[key] = c.get(key, 0) + amount
                self.db.execute("UPDATE runs SET counters=? WHERE run_id=?", (j(c), t))
            limit_key = {"tool_calls": "max_tool_calls", "model_tokens": "max_model_tokens",
                         "cost_usd": "max_cost_usd", "fanout": "max_fanout"}[key]
            if c[key] > lim[limit_key]:
                raise LimitExceeded(f"{limit_key}={lim[limit_key]} exceeded ({key}={c[key]}) at {'root' if t != rid else 'run'} {t}")

    def _acquire(self, rid: str) -> bool:
        with self.db.tx():
            r = self.db.one("SELECT lease_owner, lease_until, state FROM runs WHERE run_id=?", (rid,))
            if r["state"] in TERMINAL:
                return False
            if r["lease_owner"] and r["lease_owner"] != self.worker and (r["lease_until"] or 0) > now():
                return False
            self.db.execute("UPDATE runs SET lease_owner=?, lease_until=? WHERE run_id=?",
                            (self.worker, now() + self.lease_s, rid))
        return True

    def _release(self, rid: str) -> None:
        self.db.execute("UPDATE runs SET lease_owner=NULL, lease_until=NULL WHERE run_id=? AND lease_owner=?",
                        (rid, self.worker))

    def provide_approvals(self, rid: str, step_id: str, approval_ids: list[str]) -> None:
        r = self.get(rid)
        inp = r["input"]
        inp.setdefault("approvals", {})[step_id] = approval_ids
        self.db.execute("UPDATE runs SET input=? WHERE run_id=?", (j(inp), rid))

    def cancel(self, rid: str, actor: str, reason: str = "operator cancel") -> list[str]:
        cancelled, frontier = [], [rid]
        while frontier:
            cur = frontier.pop()
            r = self.db.one("SELECT state FROM runs WHERE run_id=?", (cur,))
            if r and r["state"] not in TERMINAL:
                self._set(cur, "CANCELLED", f"{reason} by {actor}")
                cancelled.append(cur)
            frontier += [c["run_id"] for c in self.db.all("SELECT run_id FROM runs WHERE parent_id=?", (cur,))]
        return cancelled

    # ---------------- execution ----------------
    def run(self, rid: str, max_steps: int = 1000) -> str:
        if not self._acquire(rid):
            return self.get(rid)["state"]
        try:
            res = self._loop(rid, max_steps)
        except SimulatedCrash:
            raise  # the worker "died": no cleanup, lease left to expire
        except BaseException:
            self._release(rid)
            raise
        self._release(rid)
        return res

    def _loop(self, rid: str, max_steps: int) -> str:
        run = self.db.one("SELECT * FROM runs WHERE run_id=?", (rid,))
        wf = self.workflows[run["workflow"]]
        lim = unj(run["limits"])
        if run["state"] in ("PENDING", "WAITING_APPROVAL", "BLOCKED"):
            self._set(rid, "RUNNING")
        for _ in range(max_steps):
            run = self.db.one("SELECT * FROM runs WHERE run_id=?", (rid,))
            if run["state"] in TERMINAL:
                return run["state"]
            if now() - run["created"] > lim["max_elapsed_s"]:
                self._set(rid, "BLOCKED", f"max_elapsed_s={lim['max_elapsed_s']} exceeded")
                return "BLOCKED"
            steps = {s["step_id"]: s for s in self.db.all("SELECT * FROM steps WHERE run_id=?", (rid,))}
            if all(s["state"] == "SUCCEEDED" for s in steps.values()):
                self._set(rid, "SUCCEEDED")
                return "SUCCEEDED"
            ready = [s for s in wf.steps if steps[s.id]["state"] in ("PENDING", "RUNNING", "WAITING_APPROVAL")
                     and all(steps[d]["state"] == "SUCCEEDED" for d in s.deps)]
            if not ready:
                self._set(rid, "BLOCKED", "no runnable steps")
                return "BLOCKED"
            step = ready[0]
            outputs = {sid: unj(s["output"]) for sid, s in steps.items() if s["state"] == "SUCCEEDED"}
            ctx = RunContext(self, run, step, outputs)
            st = steps[step.id]
            if step.kind == "approval":
                req = step.approval(ctx)
                ids = ctx.input.get("approvals", {}).get(step.id, [])
                try:
                    used = self.approval_resolver(ids, req["binding"], req["roles"]) if ids else None
                except PermissionError as e:
                    used = None
                    self.ledger.record("run.approval_rejected", rid, self.worker, {"step": step.id, "reason": str(e)})
                if not used:
                    self._set_step(rid, step.id, "WAITING_APPROVAL", {"request": req})
                    self._set(rid, "WAITING_APPROVAL", f"awaiting {req['roles']} on {step.id}")
                    return "WAITING_APPROVAL"
                self._set_step(rid, step.id, "SUCCEEDED", {"approvals": used, "binding": req["binding"]})
                continue
            attempts = st["attempts"] + 1
            if attempts > min(step.max_attempts, lim["max_attempts"]):
                self._set_step(rid, step.id, "FAILED")
                self._set(rid, "FAILED", f"step {step.id} exhausted attempts")
                return "FAILED"
            self._set_step(rid, step.id, "RUNNING", attempts=attempts)
            ctx.attempt = attempts
            try:
                out = step.fn(ctx)
            except StepBlocked as e:
                self._set_step(rid, step.id, "BLOCKED", {"reason": str(e)})
                self._set(rid, "BLOCKED", f"{step.id}: {e}")
                self.tm.metric("delivery", "run_blocked", 1, step=step.id, category="validation_failed")
                return "BLOCKED"
            except LimitExceeded as e:
                self._set_step(rid, step.id, "BLOCKED", {"reason": str(e)})
                self._set(rid, "BLOCKED", f"limit: {e}")
                self.tm.metric("delivery", "run_limit", 1, step=step.id, category="budget_exceeded")
                return "BLOCKED"
            except (StepFailed, Exception) as e:  # noqa: BLE001 — bounded retry, recorded
                if isinstance(e, PermissionError):
                    self._set_step(rid, step.id, "BLOCKED", {"reason": str(e)})
                    self._set(rid, "BLOCKED", f"{step.id}: denied: {e}")
                    return "BLOCKED"
                self.ledger.record("run.step_error", rid, self.worker, {"step": step.id, "attempt": attempts,
                                                                          "error": str(e)[:500],
                                                                          "trace": traceback.format_exc()[-800:]})
                self._set_step(rid, step.id, "PENDING")
                continue
            self._set_step(rid, step.id, "SUCCEEDED", out)
            self.ledger.record("run.step_succeeded", rid, self.worker, {"step": step.id, "attempt": attempts})
        return self.get(rid)["state"]

    def recover(self) -> list[str]:
        """Called on worker start: resume runs whose lease expired (crashed workers)."""
        resumed = []
        for r in self.db.all("SELECT run_id FROM runs WHERE state='RUNNING' AND (lease_until IS NULL OR lease_until < ?)",
                             (now(),)):
            self.ledger.record("run.recovered", r["run_id"], self.worker, {})
            self.run(r["run_id"]); resumed.append(r["run_id"])
        return resumed
