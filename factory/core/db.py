"""Relational store for project metadata, approvals, durable workflow state and evidence.

SQLite (WAL) is used for the reference implementation; the schema is portable to
PostgreSQL (Azure Database for PostgreSQL in the reference environment).
"""
from __future__ import annotations

import json
import re
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
CREATE TABLE IF NOT EXISTS onto_concepts(name TEXT, version INTEGER, body TEXT, digest TEXT, semantic_digest TEXT,
  status TEXT, created REAL, created_by TEXT, proposal TEXT, PRIMARY KEY(name, version));
CREATE TABLE IF NOT EXISTS onto_proposals(id TEXT PRIMARY KEY, kind TEXT, target TEXT, payload TEXT, payload_digest TEXT,
  provenance TEXT, proposer TEXT, proposer_kind TEXT, tier INTEGER, roles TEXT, status TEXT, validation TEXT,
  approvals TEXT, decided_by TEXT, reason TEXT, created REAL, decided REAL, target_version INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS action_requests(id TEXT PRIMARY KEY, action TEXT, actor TEXT, subject TEXT,
  params TEXT, env TEXT, tenant TEXT, status TEXT, roles TEXT, approvals TEXT, rule_results TEXT, result TEXT,
  created REAL, decided REAL);
CREATE TABLE IF NOT EXISTS holds_sor(hold_id TEXT PRIMARY KEY, op_id TEXT UNIQUE, tenant TEXT, account_id TEXT,
  amount_cents INTEGER, reason TEXT, status TEXT, created REAL);
CREATE TABLE IF NOT EXISTS advice_records_sor(recommendation_id TEXT PRIMARY KEY, op_id TEXT UNIQUE, tenant TEXT,
  account_id TEXT, product_code TEXT, side TEXT, amount_cents INTEGER, rationale TEXT, snapshot_id TEXT, created REAL);
CREATE TABLE IF NOT EXISTS return_registry_sor(certification_id TEXT PRIMARY KEY, op_id TEXT UNIQUE, tenant TEXT,
  entity_id TEXT, return_code TEXT, period_end TEXT, package_digest TEXT, snapshot_id TEXT, created REAL);
CREATE TABLE IF NOT EXISTS filings_sor(filing_id TEXT PRIMARY KEY, op_id TEXT UNIQUE, tenant TEXT, entity_id TEXT,
  certification_id TEXT, return_code TEXT, period_end TEXT, package_digest TEXT, receipt TEXT, created REAL);
CREATE TABLE IF NOT EXISTS risk_cases_sor(case_id TEXT PRIMARY KEY, op_id TEXT UNIQUE, tenant TEXT, account_id TEXT,
  case_type TEXT, alert_id TEXT, score REAL, reason_codes TEXT, status TEXT, created REAL);
CREATE TABLE IF NOT EXISTS restrictions_sor(restriction_id TEXT PRIMARY KEY, op_id TEXT UNIQUE, tenant TEXT, account_id TEXT,
  case_id TEXT, restriction TEXT, duration_hours INTEGER, status TEXT, created REAL);
CREATE TABLE IF NOT EXISTS limit_changes_sor(change_id TEXT PRIMARY KEY, op_id TEXT UNIQUE, tenant TEXT, account_id TEXT,
  case_id TEXT, current_limit_cents INTEGER, new_limit_cents INTEGER, notice_days INTEGER, status TEXT, created REAL);
CREATE TABLE IF NOT EXISTS orders_sor(order_id TEXT PRIMARY KEY, op_id TEXT UNIQUE, tenant TEXT, account_id TEXT,
  recommendation_id TEXT, product_code TEXT, side TEXT, amount_cents INTEGER, status TEXT, created REAL);
"""


class DB:
    """SQLite store (dev profile). PostgresDB below implements the same interface for production."""
    dialect = "sqlite"

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

    def ensure_schema(self, ddl: str) -> None:
        with self._lock:
            self.conn.executescript(ddl)

    def lock_ledger(self) -> None:
        """Serialize evidence-chain appends. SQLite: BEGIN IMMEDIATE already holds the write lock."""

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


# ------------------------------------------------------------------ PostgreSQL
_INSERT_RE = re.compile(r"^\s*INSERT\s+OR\s+(IGNORE|REPLACE)\s+INTO\s+(\w+)\s*\(([^)]*)\)", re.I | re.S)


def primary_keys(schema: str = SCHEMA) -> dict[str, list[str]]:
    keys: dict[str, list[str]] = {}
    for m in re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\((.*?)\);", schema, re.S):
        table, body = m.group(1), m.group(2)
        pk = re.search(r"PRIMARY KEY\(([^)]*)\)", body)
        if pk:
            keys[table] = [c.strip() for c in pk.group(1).split(",")]
        else:
            col = re.search(r"(\w+) \w+ PRIMARY KEY", body)
            keys[table] = [col.group(1)] if col else []
    return keys


_PK = primary_keys()


def translate(sql: str) -> str:
    """SQLite dialect used by the factory -> PostgreSQL. Pure function, unit-tested without a server."""
    out = sql.replace("?", "%s")
    m = _INSERT_RE.match(out)
    if m:
        mode, table, cols = m.group(1).upper(), m.group(2), [c.strip() for c in m.group(3).split(",")]
        out = _INSERT_RE.sub(f"INSERT INTO {table}({m.group(3)})", out, count=1).rstrip().rstrip(";")
        if mode == "IGNORE":
            out += " ON CONFLICT DO NOTHING"
        else:
            pk = _PK.get(table) or []
            upd = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in pk)
            out += f" ON CONFLICT ({', '.join(pk)}) DO UPDATE SET {upd}" if upd else f" ON CONFLICT ({', '.join(pk)}) DO NOTHING"
    return out


def pg_ddl(schema: str = SCHEMA) -> str:
    """Generate the PostgreSQL DDL for migrations/postgres/001_init.sql from the canonical schema."""
    ddl = schema.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY")
    ddl = re.sub(r"\bREAL\b", "DOUBLE PRECISION", ddl)
    return "-- GENERATED from factory/core/db.py SCHEMA by factory.cli migrate --emit. Do not edit by hand.\n" + ddl.strip() + "\n"


class PostgresDB:
    """PostgreSQL store for the production profile (Azure Database for PostgreSQL Flexible Server).

    Requires `psycopg` (v3). Schema is managed by migrations (migrations/postgres/*.sql), not at
    runtime. Statements are written in the factory's SQLite dialect and translated by `translate`.
    NOTE: not executed in the reference build environment (no PostgreSQL server or psycopg there);
    the translator and generated DDL are unit-tested, the live path is exercised in Phase 0.
    """
    dialect = "postgresql"

    def __init__(self, url: str, password_provider=None):
        import psycopg  # noqa: F401  (lazy: only needed in production)
        from psycopg.rows import dict_row
        self._psycopg, self._dict_row = psycopg, dict_row
        self.url, self._pw = url, password_provider
        self._local = threading.local()
        self._lock = threading.RLock()

    def _conn(self):
        c = getattr(self._local, "conn", None)
        if c is None or c.closed:
            kw = {"password": self._pw()} if self._pw else {}   # Entra token as password for AAD auth
            c = self._psycopg.connect(self.url, autocommit=True, row_factory=self._dict_row, **kw)
            self._local.conn = c
        return c

    def execute(self, sql: str, params: tuple | list = ()):
        cur = self._conn().cursor()
        cur.execute(translate(sql), tuple(params))
        return cur

    def one(self, sql, params=()):
        r = self.execute(sql, params).fetchone()
        return dict(r) if r else None

    def all(self, sql, params=()):
        return [dict(r) for r in self.execute(sql, params).fetchall()]

    def tx(self):
        db = self

        class _PgTx:
            def __enter__(self_):
                self_.t = db._conn().transaction()
                self_.t.__enter__()
                return db

            def __exit__(self_, et, ev, tb):
                return self_.t.__exit__(et, ev, tb)
        return _PgTx()

    def ensure_schema(self, ddl: str) -> None:
        """No-op: production schema changes go through reviewed migrations."""

    def lock_ledger(self) -> None:
        self.execute("SELECT pg_advisory_xact_lock(7340001)")

    def close(self):
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()


def open_db(url: str | None, default_path: Path, password_provider=None):
    if url and url.startswith(("postgresql://", "postgres://")):
        return PostgresDB(url, password_provider)
    if url and url.startswith("sqlite:///"):
        return DB(Path(url[len("sqlite:///"):]))
    return DB(default_path)


def j(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, default=str)


def unj(s: str | None, default: Any = None) -> Any:
    return json.loads(s) if s else default
