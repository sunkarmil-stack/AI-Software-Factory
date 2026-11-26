"""Memory tiers (REQ 027, REQ 028).

transient  : per-task context, dropped at task end, never shared.
checkpoint : workflow state (owned by the orchestrator; see orchestrator.engine).
persistent : reusable knowledge; entering it requires provenance + validation and is
             scoped to (tenant, project). It can never override an approved spec.
"""
from __future__ import annotations

from ..core.db import DB, j, unj
from ..core.evidence import Ledger
from ..core.util import new_id, now

RETENTION_DAYS = {"transient": 1, "persistent": 365}


class MemoryStore:
    def __init__(self, db: DB, ledger: Ledger):
        self.db, self.ledger = db, ledger

    def put_transient(self, run_id: str, tenant: str, project: str, data: dict) -> str:
        mid = new_id("MEM")
        self.db.execute("INSERT INTO memory(id,kind,scope,owner,project,tenant,data,provenance,status,retention_until,created)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?)", (mid, "transient", run_id, run_id, project, tenant, j(data),
                                                            j({"run": run_id}), "active", now() + 86400, now()))
        return mid

    def clear_transient(self, run_id: str) -> int:
        return self.db.execute("DELETE FROM memory WHERE kind='transient' AND scope=?", (run_id,)).rowcount

    def propose_persistent(self, tenant: str, project: str, fact: str, provenance: dict, owner: str) -> str:
        mid = new_id("MEM")
        self.db.execute("INSERT INTO memory(id,kind,scope,owner,project,tenant,data,provenance,status,retention_until,created)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?)", (mid, "persistent", f"{tenant}/{project}", owner, project,
                                                            tenant, j({"fact": fact}), j(provenance), "quarantined",
                                                            now() + 365 * 86400, now()))
        return mid

    def promote(self, mid: str, validator: str, *, source_exists: bool, same_project: bool, spec_conflict: bool) -> str:
        r = self.db.one("SELECT * FROM memory WHERE id=?", (mid,))
        prov = unj(r["provenance"])
        ok = bool(prov.get("source")) and source_exists and same_project and not spec_conflict
        status = "active" if ok else "rejected"
        self.db.execute("UPDATE memory SET status=? WHERE id=?", (status, mid))
        self.ledger.record("memory.promotion", mid, validator, {"status": status, "provenance": prov}, r["project"])
        return status

    def recall(self, tenant: str, project: str) -> list[dict]:
        rows = self.db.all("SELECT * FROM memory WHERE kind='persistent' AND status='active' AND tenant=? AND project=?",
                           (tenant, project))
        return [{"id": r["id"], "fact": unj(r["data"])["fact"], "origin": unj(r["provenance"])} for r in rows]
