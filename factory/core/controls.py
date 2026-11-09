"""Emergency controls / kill switch (REQ 053, NFR 06).

Gateways consult a cached control snapshot. If the snapshot is older than
`max_age_s` and cannot be refreshed, enforcement fails closed.
"""
from __future__ import annotations

import fnmatch
import threading

from .db import DB
from .evidence import Ledger
from .util import new_id, now

KINDS = {"cancel_run", "revoke_tool", "narrow_model", "disable_capability", "revoke_skill"}


class ControlsUnavailable(RuntimeError):
    pass


class Controls:
    def __init__(self, db: DB, ledger: Ledger, refresh_s: float = 5.0, max_age_s: float = 60.0):
        self.db, self.ledger = db, ledger
        self.refresh_s, self.max_age_s = refresh_s, max_age_s
        self._snap: list[dict] = []
        self._snap_ts = 0.0
        self._lock = threading.Lock()
        self.store_down = False  # fault injection

    def activate(self, kind: str, target: str, actor: str, reason: str) -> str:
        if kind not in KINDS:
            raise ValueError(kind)
        cid = new_id("CTL")
        self.db.execute("INSERT INTO controls(id,kind,target,actor,reason,active,ts) VALUES(?,?,?,?,?,1,?)",
                        (cid, kind, target, actor, reason, now()))
        self.ledger.record("ops.control.activated", cid, actor, {"kind": kind, "target": target, "reason": reason})
        return cid

    def deactivate(self, cid: str, actor: str, reason: str) -> None:
        self.db.execute("UPDATE controls SET active=0 WHERE id=?", (cid,))
        self.ledger.record("ops.control.deactivated", cid, actor, {"reason": reason})

    def snapshot(self) -> list[dict]:
        with self._lock:
            age = now() - self._snap_ts
            if age >= self.refresh_s:
                if not self.store_down:
                    self._snap = self.db.all("SELECT * FROM controls WHERE active=1")
                    self._snap_ts = now()
                elif age > self.max_age_s:
                    raise ControlsUnavailable("control snapshot stale beyond max age; failing closed")
            return self._snap

    def force_refresh(self) -> None:
        with self._lock:
            self._snap_ts = 0.0

    def blocked(self, kind: str, subject: str) -> dict | None:
        for c in self.snapshot():
            if c["kind"] == kind and fnmatch.fnmatchcase(subject, c["target"]):
                return c
        return None
