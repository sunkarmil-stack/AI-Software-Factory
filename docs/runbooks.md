# Runbooks (REQ 053, REQ 055, REQ 074, NFR 05)

**Kill a capability** — `POST /v1/ops/controls {"kind":"revoke_tool|disable_capability|narrow_model|cancel_run","target":..,"reason":..}` (operator). Gateways refresh every 5 s and fail closed when the snapshot is older than 60 s. Then list in-flight ops: `SELECT * FROM operations WHERE state IN ('pending','unknown')` and reconcile each against the system of record before any retry.

**Stuck run** — `GET /v1/runs/{id}`: `WAITING_APPROVAL` → chase approver; `BLOCKED` → read `error` (ambiguity, limit, denial); `RUNNING` with expired lease → start a worker and call `Engine.recover()`. Never edit step state by hand.

**Dead-letter replay** — operator only: `ToolGateway.replay(dlq_id, operator, ctx)`; authorization and approvals are re-evaluated, so stale approvals fail again by design.

**Evidence integrity** — `python -m factory.cli verify-evidence`. A failure gives the first bad sequence number; freeze promotions and open a security incident.

**Rollback** — failing canary rolls back automatically to the previous digest. For a manual rollback, promote the previous approved release (fresh approval required). Data migrations need their own recovery plan.

**Recovery exercise** — quarterly, run by a team other than the authors: kill a worker mid-run (see `test_acc04`), restore `factory.db` + `objects/` from backup into a new `FACTORY_HOME`, run `verify-evidence` and `Engine.recover()`, and confirm no duplicate downstream writes.

## Production runbooks

**RB-11 Two-layer kill switch** — Choose the layer by who must act and how wide the stop is.
- *Factory layer* (factory operator, per capability/tool/model/run): `POST /v1/ops/controls` as above.
- *Enterprise layer* (AI gateway owner, no factory deployment needed): set APIM named value `factory-enabled=false`
  (all factory egress), or add an alias to `revoked-models` / an API name to `revoked-apis`. The factory records
  `model.enterprise_revoked` / `tool.enterprise_revoked` evidence, uses only approved fallbacks and never retries a
  revoked SoR write. Use both layers for a suspected compromise. Re-enable in reverse order and reconcile
  `operations` in state `unknown` first.

**RB-12 Preflight failure at start** — Pods crash-loop with `production preflight failed: <check>: <detail>`. Fix the
control (never the check). An emergency exemption needs an owner, reason and `expires` (epoch, ≤ 7 days), is merged
through the environments repo with change-manager approval, and appears in `/readyz` and `factory.preflight` evidence.

**RB-13 Database restore (NFR 05)** — Point-in-time restore of the Flexible Server to a new server → run
`migrate --apply` (no-op if current) → point `FACTORY_DATABASE_URL` at it via the environments repo → scale workers
to 0, start one worker, run `python -m factory.cli verify-evidence` and `Engine.recover()` → reconcile `unknown`
operations against systems of record → scale out. Record RPO/RTO achieved.

**RB-14 Key rotation / compromise** — Rotation is automatic (Key Vault policy); old versions stay enabled for
verification. On compromise: disable the key *version*, create a new version, freeze promotions, re-sign pending
releases, and list artifacts signed by the bad version from evidence (`kind in (build.provenance, release.created)`).

**RB-15 Enterprise gateway throttling** — Sustained 429s: check `llm-token-limit` metrics by `Workload`/`Project`;
the factory fails over to the approved fallback and then queues. Raise the quota through the gateway owner; do not
add unapproved fallbacks.
