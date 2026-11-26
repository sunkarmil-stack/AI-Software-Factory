"""Audit reconstruction & export (REQ 001, REQ 050, ACC 09).

Read-only: builds the requirement -> test -> artifact -> deployment chain from immutable
references and the hash-chained ledger. Sensitive payloads are not included; only digests
and references (auditors get evidence, not unnecessary content).
"""
from __future__ import annotations

from ..core.db import unj


def reconstruct(f, release_id: str) -> dict:
    rel = f.release.get(release_id)
    m = rel["manifest"]
    rn = f.db.one("SELECT id FROM trace_nodes WHERE kind='RELEASE' AND ref=?", (release_id,))["id"]
    chain = f.trace.closure(rn)
    deps = f.db.all("SELECT * FROM deployments WHERE release_id=?", (release_id,))
    approvals = []
    for n in f.db.all("SELECT src FROM trace_edges WHERE dst=? AND rel='authorizes'", (rn,)):
        aid = f.trace.get(n["src"])["ref"]
        a = f.approvals.get(aid)
        approvals.append({"id": aid, "approver": a["approver"], "role": a["role"], "binding": a["binding"],
                          "valid_now": f.approvals.check(aid, a["binding"], a["role"]).ok})
    build = f.ci.build(m["evidence"]["build"])
    skills, models = set(), set()
    for e in f.ledger.query(kind="agent.output", project=rel["project"]):
        p = e["data"].get("prompt") or {}
        for s, d in (p.get("skills") or {}).items():
            skills.add(f"{s}={d}")
        models.add(f"{e['data'].get('deployment')}@{e['data'].get('revision')}")
    ok, n = f.ledger.verify_chain()
    by_kind: dict[str, list] = {}
    for c in chain:
        by_kind.setdefault(c["kind"], []).append({"ref": c["ref"], "status": c["status"], "digest": c["digest"]})
    return {
        "release": release_id, "manifest_digest": rel["manifest_digest"],
        "intent": {"spec": m["spec"], "requirements": by_kind.get("REQ", []), "outcomes": by_kind.get("BO", [])},
        "source": m["source"], "artifact": m["artifact"],
        "provenance_verified": not f.ci.verify_provenance(build, m["source"]["commit"]),
        "checks": m["evidence"]["checks"], "tests": by_kind.get("TEST", []),
        "skills": sorted(skills), "models": sorted(models), "policy": m["policy"],
        "approvals": approvals, "deployments": [{"id": d["id"], "env": d["env"], "status": d["status"],
                                                  "digest": d["artifact_digest"]} for d in deps],
        "limitations": m["evidence"]["limitations"],
        "chain": chain, "evidence_chain_valid": ok, "evidence_records": n,
    }


def by_digest(f, digest: str) -> dict:
    d = f.db.one("SELECT release_id FROM deployments WHERE artifact_digest=? ORDER BY created DESC LIMIT 1", (digest,))
    if not d:
        raise KeyError(digest)
    return reconstruct(f, d["release_id"])


def export(f, project: str) -> list[dict]:
    rows = f.ledger.query(project=project)
    return [{k: r[k] for k in ("id", "kind", "subject", "actor", "ts", "digest", "prev_hash", "hash")} | {"data": r["data"]}
            for r in rows]
