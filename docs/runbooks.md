# Runbooks (REQ 053, REQ 055)

**Kill a capability** — `POST /v1/ops/controls {"kind":"revoke_tool|disable_capability|narrow_model|cancel_run","target":..,"reason":..}` (operator). Gateways refresh every 5 s and fail closed when the snapshot is older than 60 s. Then list in-flight ops: `SELECT * FROM operations WHERE state IN ('pending','unknown')` and reconcile each against the system of record before any retry.

**Stuck run** — `GET /v1/runs/{id}`: `WAITING_APPROVAL` → chase approver; `BLOCKED` → read `error` (ambiguity, limit, denial); `RUNNING` with expired lease → start a worker and call `Engine.recover()`. Never edit step state by hand.

**Dead-letter replay** — operator only: `ToolGateway.replay(dlq_id, operator, ctx)`; authorization and approvals are re-evaluated, so stale approvals fail again by design.

**Evidence integrity** — `python -m factory.cli verify-evidence`. A failure gives the first bad sequence number; freeze promotions and open a security incident.

**Rollback** — failing canary rolls back automatically to the previous digest. For a manual rollback, promote the previous approved release (fresh approval required). Data migrations need their own recovery plan.

**Recovery exercise** — quarterly, run by a team other than the authors: kill a worker mid-run (see `test_acc04`), restore `factory.db` + `objects/` from backup into a new `FACTORY_HOME`, run `verify-evidence` and `Engine.recover()`, and confirm no duplicate downstream writes.
