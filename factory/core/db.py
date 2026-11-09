"""Relational store for project metadata, approvals, durable workflow state and evidence.

SQLite (WAL) is used for the reference implementation; the schema is portable to
PostgreSQL (Azure Database for PostgreSQL in the reference environment).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY, tenant TEXT, name TEXT, risk_tier INTEGER,
  budget_usd REAL, owners TEXT, template TEXT, skills TEXT, created REAL);
CREATE TABLE IF NOT EXISTS specs(spec_id TEXT, version INTEGER, digest TEXT, body TEXT, status TEXT,
  project TEXT, created REAL, created_by TEXT, PRIMARY KEY(spec_id, version));
CREATE TABLE IF NOT EXISTS trace_nodes(id TEXT PRIMARY KEY, kind TEXT, ref TEXT, digest TEXT, status TEXT,
  project TEXT, data TEXT, created REAL);
CREATE TABLE IF NOT EXISTS trace_edges(src TEXT, dst TEXT, rel TEXT, PRIMARY KEY(src, dst, rel));
CREATE TABLE IF NOT EXISTS skills(name TEXT, version TEXT, digest TEXT, signature TEXT, manifest TEXT,
  path TEXT, status TEXT, owner TEXT, published REAL, PRIMARY KEY(name, version));
CREATE TABLE IF NOT EXISTS tool_contracts(name TEXT, version TEXT, digest TEXT, contract TEXT, status TEXT,
  PRIMARY KEY(name, version));
CREATE TABLE IF NOT EXISTS agents(id TEXT, version TEXT, digest TEXT, manifest TEXT, status TEXT,
  PRIMARY KEY(id, version));
CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, parent_id TEXT, root_id TEXT, workflow TEXT,
  project TEXT, tenant TEXT, actor TEXT, state TEXT, input TEXT, limits TEXT, counters TEXT,
  created REAL, updated REAL, error TEXT, lease_owner TEXT, lease_until REAL);
CREATE TABLE IF NOT EXISTS steps(run_id TEXT, step_id TEXT, state TEXT, attempts INTEGER, output TEXT,
  updated REAL, PRIMARY KEY(run_id, step_id));
CREATE TABLE IF NOT EXISTS operations(op_id TEXT PRIMARY KEY, tool TEXT, caller TEXT, payload_hash TEXT,
  state TEXT, result TEXT, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS dlq(id TEXT PRIMARY KEY, tool TEXT, caller TEXT, tenant TEXT, payload TEXT,
  error TEXT, approval_id TEXT, created REAL, replayed INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY, approver TEXT, role TEXT, action TEXT,
  binding TEXT, binding_digest TEXT, expires REAL, signature TEXT, created REAL, revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS review_items(id TEXT PRIMARY KEY, project TEXT, kind TEXT, ref TEXT,
  summary TEXT, status TEXT, data TEXT, created REAL);
CREATE TABLE IF NOT EXISTS evidence(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, kind TEXT,
  subject TEXT, actor TEXT, project TEXT, data TEXT, digest TEXT, prev_hash TEXT, hash TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS controls(id TEXT PRIMARY KEY, kind TEXT, target TEXT, actor TEXT, reason TEXT,
  active INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS usage(id TEXT PRIMARY KEY, root_run TEXT, run TEXT, project TEXT, tenant TEXT,
  kind TEXT, units REAL, cost REAL, latency_ms REAL, ts REAL);
CREATE TABLE IF NOT EXISTS prs(id TEXT PRIMARY KEY, project TEXT, branch TEXT, head_sha TEXT, base TEXT,
  author TEXT, status TEXT, title TEXT, created REAL, merged_sha TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS builds(id TEXT PRIMARY KEY, project TEXT, source_sha TEXT, artifact_digest TEXT,
  provenance TEXT, status TEXT, report TEXT, created REAL);
CREATE TABLE IF NOT EXISTS releases(id TEXT PRIMARY KEY, project TEXT, manifest TEXT, manifest_digest TEXT,
  signature TEXT, status TEXT, created REAL);
CREATE TABLE IF NOT EXISTS deployments(id TEXT PRIMARY KEY, release_id TEXT, project TEXT, env TEXT,
  artifact_digest TEXT, status TEXT, data TEXT, created REAL);
CREATE TABLE IF NOT EXISTS metrics(id INTEGER PRIMARY KEY AUTOINCREMENT, plane TEXT, name TEXT,
  labels TEXT, value REAL, ts REAL);
CREATE TABLE IF NOT EXISTS memory(id TEXT PRIMARY KEY, kind TEXT, scope TEXT, owner TEXT, project TEXT,
  tenant TEXT, data TEXT, provenance TEXT, status TEXT, retention_until REAL, created REAL);
CREATE TABLE IF NOT EXISTS docs(id TEXT, version INTEGER, tenant TEXT, project TEXT, acl TEXT,
  classification TEXT, title TEXT, text TEXT, source TEXT, deleted INTEGER DEFAULT 0, updated REAL,
  PRIMARY KEY(id, version));
CREATE TABLE IF NOT EXISTS backlog(id TEXT PRIMARY KEY, project TEXT, source TEXT, title TEXT,
  detail TEXT, status TEXT, created REAL);
"""


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None,
                                    timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)

    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.conn.execute(sql, params)

    def tx(self):
        return _Tx(self)

    def one(self, sql: str, params: tuple | list = ()) -> dict | None:
        with self._lock:
            r = self.conn.execute(sql, params).fetchone()
        return dict(r) if r else None

    def all(self, sql: str, params: tuple | list = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def close(self):
        with self._lock:
            self.conn.close()


class _Tx:
    def __init__(self, db: DB):
        self.db = db

    def __enter__(self):
        self.db._lock.acquire()
        self.db.conn.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, et, ev, tb):
        try:
            self.db.conn.execute("COMMIT" if et is None else "ROLLBACK")
        finally:
            self.db._lock.release()
        return False


def j(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def unj(s: str | None, default: Any = None) -> Any:
    return json.loads(s) if s else default
