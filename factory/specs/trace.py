"""Traceability graph with dependency-driven invalidation (REQ 001, REQ 004, ACC 01, ACC 09).

Edges point from a dependent artifact to what it depends on (e.g. TEST -> AC, RELEASE -> BUILD).
When a node is superseded, every transitive dependent is marked stale.
"""
from __future__ import annotations

from ..core.db import DB, j, unj
from ..core.util import now


class Trace:
    def __init__(self, db: DB):
        self.db = db

    def node(self, kind: str, ref: str, digest: str, project: str, data: dict | None = None) -> str:
        nid = f"{kind}:{ref}:{digest.split(':')[-1][:12]}"
        self.db.execute("INSERT OR IGNORE INTO trace_nodes(id,kind,ref,digest,status,project,data,created)"
                        " VALUES(?,?,?,?,?,?,?,?)", (nid, kind, ref, digest, "current", project, j(data or {}), now()))
        return nid

    def edge(self, src: str, dst: str, rel: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO trace_edges(src,dst,rel) VALUES(?,?,?)", (src, dst, rel))

    def get(self, nid: str) -> dict | None:
        r = self.db.one("SELECT * FROM trace_nodes WHERE id=?", (nid,))
        if r:
            r["data"] = unj(r["data"])
        return r

    def current(self, kind: str, ref: str, project: str) -> dict | None:
        return self.db.one("SELECT * FROM trace_nodes WHERE kind=? AND ref=? AND project=? AND status='current'"
                           " ORDER BY created DESC LIMIT 1", (kind, ref, project))

    def dependents(self, nid: str) -> list[dict]:
        return self.db.all("SELECT * FROM trace_edges WHERE dst=?", (nid,))

    def dependencies(self, nid: str) -> list[dict]:
        return self.db.all("SELECT * FROM trace_edges WHERE src=?", (nid,))

    def supersede(self, nid: str, reason: str) -> list[str]:
        """Mark node superseded and all transitive dependents stale. Returns affected ids."""
        self.db.execute("UPDATE trace_nodes SET status='superseded' WHERE id=?", (nid,))
        affected, frontier, seen = [], [nid], {nid}
        while frontier:
            cur = frontier.pop()
            for e in self.dependents(cur):
                s = e["src"]
                if s in seen:
                    continue
                seen.add(s)
                self.db.execute("UPDATE trace_nodes SET status='stale' WHERE id=? AND status='current'", (s,))
                affected.append(s)
                frontier.append(s)
        return affected

    def closure(self, nid: str) -> list[dict]:
        """All nodes the given node transitively depends on (for audit reconstruction)."""
        out, frontier, seen = [], [nid], {nid}
        while frontier:
            cur = frontier.pop()
            for e in self.dependencies(cur):
                d = e["dst"]
                if d in seen:
                    continue
                seen.add(d)
                n = self.get(d)
                if n:
                    n["rel_from"] = cur
                    n["rel"] = e["rel"]
                    out.append(n)
                frontier.append(d)
        return out

    def stale_in_closure(self, nid: str) -> list[str]:
        me = self.get(nid)
        bad = [n["id"] for n in self.closure(nid) if n["status"] != "current"]
        if me and me["status"] != "current":
            bad.insert(0, nid)
        return bad
