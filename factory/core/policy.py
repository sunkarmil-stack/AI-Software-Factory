"""Executable, versioned, testable policy enforced outside model reasoning (REQ 047, Section 10).

Decision procedure: deny-overrides; default deny. The autonomy tier is computed from the
*action* (side effect, environment, data class, reversibility), never from the agent's name,
and an agent cannot supply its own tier (any `tier` in the request is ignored).
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .util import digest_obj


def _get(req: dict, path: str) -> Any:
    cur: Any = req
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _match_value(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        for op, v in expected.items():
            if op == "in" and actual not in v: return False
            if op == "not_in" and actual in v: return False
            if op == "gte" and (actual is None or actual < v): return False
            if op == "lte" and (actual is None or actual > v): return False
            if op == "gt" and (actual is None or actual <= v): return False
            if op == "exists" and (actual is not None) != bool(v): return False
            if op == "not" and _match_value(actual, v): return False
            if op == "contains" and (not isinstance(actual, (list, set, tuple)) or v not in actual): return False
        return True
    if isinstance(expected, list):
        return any(_match_value(actual, e) for e in expected)
    if isinstance(expected, str) and any(c in expected for c in "*?[") and isinstance(actual, str):
        return fnmatch.fnmatchcase(actual, expected)
    return actual == expected


def matches(req: dict, cond: dict) -> bool:
    return all(_match_value(_get(req, k), v) for k, v in (cond or {}).items())


@dataclass
class Decision:
    allow: bool
    tier: int
    reasons: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    obligations: dict = field(default_factory=dict)
    policy_digest: str = ""


class PolicyEngine:
    def __init__(self, doc: dict):
        self.doc = doc
        self.policy_id = doc["policy_id"]
        self.version = doc["version"]
        self.digest = digest_obj(doc)

    @classmethod
    def load(cls, path: Path) -> "PolicyEngine":
        return cls(yaml.safe_load(Path(path).read_text(encoding="utf-8")))

    def classify(self, req: dict) -> int:
        tier = 0
        for c in self.doc.get("classification", []):
            if matches(req, c["when"]):
                tier = max(tier, int(c["tier"]))
        return tier

    def evaluate(self, req: dict) -> Decision:
        req = dict(req)
        req.pop("tier", None)  # agents cannot choose their own tier
        tier = self.classify(req)
        req["tier"] = tier
        allow_hits, deny_hits, reasons = [], [], []
        obligations: dict = {}
        for r in self.doc.get("rules", []):
            if matches(req, r.get("when", {})):
                if r["effect"] == "deny":
                    deny_hits.append(r["id"]); reasons.append(r.get("reason", r["id"]))
                elif r["effect"] == "allow":
                    allow_hits.append(r["id"])
                    for k, v in (r.get("obligations") or {}).items():
                        obligations.setdefault(k, v)
        for o in self.doc.get("obligations", []):
            if matches(req, o["when"]):
                for k, v in o["require"].items():
                    obligations[k] = v
        if deny_hits:
            return Decision(False, tier, reasons, deny_hits, obligations, self.digest)
        if allow_hits:
            return Decision(True, tier, [], allow_hits, obligations, self.digest)
        return Decision(False, tier, ["default deny: no allow rule matched"], [], obligations, self.digest)

    def run_tests(self, tests: list[dict]) -> list[str]:
        """Return failures. Removing a required control is itself a failure."""
        failures = []
        ids = {r["id"] for r in self.doc.get("rules", [])}
        for rc in self.doc.get("required_controls", []):
            if rc not in ids:
                failures.append(f"required control removed: {rc}")
        for t in tests:
            d = self.evaluate(t["request"])
            want = t["expect"] == "allow"
            if d.allow != want:
                failures.append(f"{t['name']}: expected {t['expect']}, got {'allow' if d.allow else 'deny'} {d.rules}")
            if "tier" in t and d.tier != t["tier"]:
                failures.append(f"{t['name']}: expected tier {t['tier']}, got {d.tier}")
        return failures
