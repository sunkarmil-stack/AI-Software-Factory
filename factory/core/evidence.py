"""Evidence registry: append-only hash-chained ledger + content-addressed object store.

REQ 050 (audit linkage), REQ 051 (redaction/minimization), NFR 07 (evidence write failure
must not silently permit a consequential action — callers use `record_required`).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .db import DB, j, unj
from .util import canonical_json, digest_obj, new_id, now, sha256_bytes

SECRET_PATTERNS = [
    (re.compile(r"(?i)(api[_-]?key|secret|password|token|passwd)(\"?\s*[:=]\s*\"?)([^\s\"',}]{6,})"), r"\1\2[REDACTED]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED-AWS-KEY]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "[REDACTED-PRIVATE-KEY]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED-SSN]"),
    (re.compile(r"\b\d{3}[- ]\d{3}[- ]\d{3}\b"), "[REDACTED-SIN]"),
    (re.compile(r"\b(?:\d[ -]?){13,16}\b"), "[REDACTED-PAN]"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[REDACTED-EMAIL]"),
    (re.compile(r"(?i)bearer\s+[a-z0-9._\-]{10,}"), "Bearer [REDACTED]"),
]


def redact(value: Any) -> Any:
    if isinstance(value, str):
        for pat, rep in SECRET_PATTERNS:
            value = pat.sub(rep, value)
        return value
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if k.lower() in {"password", "secret", "api_key", "token", "authorization"}
                    else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class EvidenceWriteError(RuntimeError):
    pass


class ObjectStore:
    """Content-addressed, write-once object storage (Azure Blob immutable containers in prod)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes) -> str:
        d = sha256_bytes(data)
        p = self.root / d.split(":")[1]
        if not p.exists():
            p.write_bytes(data)
            p.chmod(0o444)
        return d

    def put_json(self, obj: Any) -> str:
        return self.put(canonical_json(obj))

    def get(self, digest: str) -> bytes:
        p = self.root / digest.split(":")[1]
        data = p.read_bytes()
        if sha256_bytes(data) != digest:
            raise EvidenceWriteError(f"object {digest} failed integrity check")
        return data


class Ledger:
    def __init__(self, db: DB, store: ObjectStore):
        self.db = db
        self.store = store
        self.fail_next = False  # fault injection for NFR 07 tests

    def record(self, kind: str, subject: str, actor: str, data: dict | None = None,
               project: str | None = None, protected_payload: Any = None) -> dict:
        if self.fail_next:
            self.fail_next = False
            raise EvidenceWriteError("evidence store unavailable (injected)")
        data = redact(dict(data or {}))
        if protected_payload is not None:
            data["protected_payload_ref"] = self.store.put_json(protected_payload)
        with self.db.tx():
            prev = self.db.one("SELECT hash FROM evidence ORDER BY seq DESC LIMIT 1")
            prev_hash = prev["hash"] if prev else "sha256:genesis"
            eid = new_id("EV")
            ts = now()
            body = {"id": eid, "kind": kind, "subject": subject, "actor": actor, "project": project,
                    "data": data, "ts": ts}
            digest = digest_obj(body)
            h = digest_obj({"prev": prev_hash, "digest": digest})
            self.db.execute("INSERT INTO evidence(id,kind,subject,actor,project,data,digest,prev_hash,hash,ts)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (eid, kind, subject, actor, project, j(data), digest, prev_hash, h, ts))
        return {**body, "digest": digest, "hash": h}

    def verify_chain(self) -> tuple[bool, int]:
        prev = "sha256:genesis"
        rows = self.db.all("SELECT * FROM evidence ORDER BY seq")
        for r in rows:
            body = {"id": r["id"], "kind": r["kind"], "subject": r["subject"], "actor": r["actor"],
                    "project": r["project"], "data": unj(r["data"]), "ts": r["ts"]}
            if digest_obj(body) != r["digest"] or r["prev_hash"] != prev:
                return False, r["seq"]
            if digest_obj({"prev": prev, "digest": r["digest"]}) != r["hash"]:
                return False, r["seq"]
            prev = r["hash"]
        return True, len(rows)

    def query(self, subject: str | None = None, kind: str | None = None, project: str | None = None) -> list[dict]:
        sql, p = "SELECT * FROM evidence WHERE 1=1", []
        if subject:
            sql += " AND subject=?"; p.append(subject)
        if kind:
            sql += " AND kind=?"; p.append(kind)
        if project:
            sql += " AND project=?"; p.append(project)
        rows = self.db.all(sql + " ORDER BY seq", p)
        for r in rows:
            r["data"] = unj(r["data"])
        return rows
