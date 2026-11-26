"""Bound approvals (REQ 048, REQ 049, ACC 05).

An approval binds approver identity, authority (role), action, artifact/payload digest,
environment, scope, expiry and relevant state (e.g. spec baseline digest). Any change to
those values invalidates execution; current authorization is re-checked at use time.
"""
from __future__ import annotations

from dataclasses import dataclass

from .crypto import KeyStore
from .db import DB, j, unj
from .evidence import Ledger
from .identity import Directory
from .util import canonical_json, digest_obj, new_id, now

BINDING_FIELDS = ("action", "subject_digest", "env", "scope", "state")


class ApprovalError(PermissionError):
    pass


@dataclass
class ApprovalCheck:
    ok: bool
    reason: str = ""


class ApprovalService:
    def __init__(self, db: DB, keys: KeyStore, directory: Directory, ledger: Ledger,
                 default_ttl_s: float = 8 * 3600):
        self.db, self.keys, self.dir, self.ledger = db, keys, directory, ledger
        self.ttl = default_ttl_s

    @staticmethod
    def binding(action: str, subject_digest: str, env: str, scope: str, state: dict | None = None) -> dict:
        return {"action": action, "subject_digest": subject_digest, "env": env, "scope": scope,
                "state": state or {}}

    def approve(self, approver_id: str, role: str, binding: dict, *, author_ids: set[str] | None = None,
                ttl_s: float | None = None, rationale: str = "") -> str:
        p = self.dir.get(approver_id)
        if p.kind != "human":
            raise ApprovalError(f"{approver_id} is a {p.kind}; only humans satisfy required approvals")
        if not p.has_role(role):
            raise ApprovalError(f"{approver_id} lacks authority role {role}")
        if author_ids and approver_id in author_ids:
            raise ApprovalError("independent approval required: approver authored or proposed the change")
        aid = new_id("APR")
        expires = now() + (ttl_s or self.ttl)
        bd = digest_obj({k: binding[k] for k in BINDING_FIELDS})
        payload = canonical_json({"id": aid, "approver": approver_id, "role": role, "binding_digest": bd,
                                  "expires": expires})
        sig = self.keys.sign("approval-service", payload)
        self.db.execute("INSERT INTO approvals(id,approver,role,action,binding,binding_digest,expires,signature,created)"
                        " VALUES(?,?,?,?,?,?,?,?,?)",
                        (aid, approver_id, role, binding["action"], j(binding), bd, expires, sig, now()))
        self.ledger.record("approval.granted", aid, approver_id,
                           {"role": role, "binding": binding, "binding_digest": bd, "rationale": rationale})
        return aid

    def revoke(self, aid: str, actor: str, reason: str) -> None:
        self.db.execute("UPDATE approvals SET revoked=1 WHERE id=?", (aid,))
        self.ledger.record("approval.revoked", aid, actor, {"reason": reason})

    def get(self, aid: str) -> dict | None:
        r = self.db.one("SELECT * FROM approvals WHERE id=?", (aid,))
        if r:
            r["binding"] = unj(r["binding"])
        return r

    def check(self, aid: str, expected: dict, role: str) -> ApprovalCheck:
        r = self.get(aid)
        if not r:
            return ApprovalCheck(False, "approval not found")
        payload = canonical_json({"id": r["id"], "approver": r["approver"], "role": r["role"],
                                  "binding_digest": r["binding_digest"], "expires": r["expires"]})
        if not self.keys.verify("approval-service", payload, r["signature"]):
            return ApprovalCheck(False, "approval record signature invalid (tampered)")
        if r["revoked"]:
            return ApprovalCheck(False, "approval revoked")
        if now() > r["expires"]:
            return ApprovalCheck(False, "approval expired")
        if r["role"] != role:
            return ApprovalCheck(False, f"approval role {r['role']} != required {role}")
        exp_digest = digest_obj({k: expected[k] for k in BINDING_FIELDS})
        if exp_digest != r["binding_digest"]:
            diff = [k for k in BINDING_FIELDS if r["binding"].get(k) != expected.get(k)]
            return ApprovalCheck(False, f"binding mismatch on {diff}: payload, target or state changed")
        try:
            p = self.dir.get(r["approver"])
        except KeyError:
            return ApprovalCheck(False, "approver no longer exists")
        if not p.has_role(role):
            return ApprovalCheck(False, "approver no longer holds authority (re-checked at use)")
        return ApprovalCheck(True)

    def require(self, aids: list[str], expected: dict, roles: list[str]) -> list[str]:
        """Each required role must be satisfied by a distinct valid approval. Returns used ids."""
        used, problems = [], []
        for role in roles:
            hit = None
            for aid in aids:
                if aid in used:
                    continue
                c = self.check(aid, expected, role)
                if c.ok:
                    hit = aid; break
                problems.append(f"{aid}: {c.reason}")
            if not hit:
                raise ApprovalError(f"missing valid approval for role {role}; " + "; ".join(problems))
            used.append(hit)
        approvers = [self.get(a)["approver"] for a in used]
        if len(set(approvers)) != len(approvers):
            raise ApprovalError("separation of duties: distinct approvers required per role")
        return used
