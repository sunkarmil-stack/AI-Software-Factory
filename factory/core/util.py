"""Stable identifiers, canonical digests and time helpers (REQ 001, REQ 013)."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, Iterable

_CLOCK_OFFSET = 0.0  # tests may advance time deterministically


def now() -> float:
    return time.time() + _CLOCK_OFFSET


def advance_clock(seconds: float) -> None:
    global _CLOCK_OFFSET
    _CLOCK_OFFSET += seconds


def iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts if ts is not None else now()))


def new_id(prefix: str) -> str:
    """Time-ordered unique id: PREFIX-<ms timestamp hex>-<random>."""
    return f"{prefix}-{int(time.time() * 1000):x}-{secrets.token_hex(4)}"


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_obj(obj: Any) -> str:
    return sha256_bytes(canonical_json(obj))


def iter_files(root: Path, exclude: Iterable[str] = (".git", "__pycache__", ".pytest_cache")) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and not any(part in exclude for part in p.relative_to(root).parts):
            out.append(p)
    return out


def digest_dir(root: Path) -> str:
    """Content digest of a directory tree: sorted relative paths + file bytes."""
    h = hashlib.sha256()
    for p in iter_files(root):
        rel = p.relative_to(root).as_posix().encode()
        h.update(len(rel).to_bytes(4, "big") + rel)
        data = p.read_bytes()
        h.update(len(data).to_bytes(8, "big") + data)
    return "sha256:" + h.hexdigest()


def factory_home() -> Path:
    return Path(os.environ.get("FACTORY_HOME", Path.cwd() / ".factory-home")).resolve()


REPO_ROOT = Path(__file__).resolve().parents[2]
