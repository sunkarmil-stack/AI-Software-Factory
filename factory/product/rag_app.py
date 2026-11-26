"""Bounded RAG agent application (Boundary D; Section 12 second sample).

Production agent = immutable release bundle (agent manifest + prompt + pinned skills +
tool contracts + retrieval config) + scoped workload identity + read-only tools + a
persistent human review queue. It cannot edit itself, load unapproved skills or widen
its permissions through prompts or retrieved content.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..agents.runtime import Agent
from ..core.db import unj
from ..core.util import REPO_ROOT, digest_obj
from ..tools.gateway import CallContext, ToolDenied

PROJECT, TENANT = "ops-assistant", "retail"


def load_corpus(f, path: Path | None = None) -> int:
    docs = yaml.safe_load((path or REPO_ROOT / "samples" / "rag" / "corpus.yaml").read_text(encoding="utf-8"))["documents"]
    for d in docs:
        f.retrieval.ingest(d["id"], d["tenant"], d["project"], d["acl"], d["classification"], d["title"], d["text"], d["source"])
    return len(docs)


@dataclass
class Answer:
    status: str
    answer: str
    citations: list[str]
    confidence: float
    review_id: str | None
    denied_tool_requests: list
    attempted_tools: int


class RagApp:
    def __init__(self, f, prompt_version: str = "1.0.0", env: str = "nonprod"):
        self.f, self.env = f, env
        m = copy.deepcopy(unj(f.db.one("SELECT manifest FROM agents WHERE id='rag-assistant'")["manifest"]))
        if prompt_version != m["prompt"]["version"]:
            m["prompt"]["version"] = prompt_version   # candidate under evaluation: pinned on approval
            m["prompt"].pop("digest", None)
        self.agent = Agent(f, m)
        self.bundle = {"agent": m["id"], "agent_version": m["version"], "prompts": {m["prompt"]["id"]: prompt_version},
                       "prompt_digest": digest_obj(self.agent.prompt), "skills": {s["name"]: s.get("digest") for s in m["skills"]},
                       "tools": {t: f.tools.contract_digest(t) for t in ("kb", "review_queue")},
                       "retrieval": {"k": self.agent.prompt.get("config", {}).get("top_k", 4), "fusion": "rrf(bm25,tfidf)"}}
        self.bundle_digest = digest_obj(self.bundle)

    def ask(self, user: str, groups: list[str], question: str, run_id: str = "adhoc") -> Answer:
        p = self.agent.principal
        ctx = CallContext(p, TENANT, PROJECT, run_id, self.env, purpose="product")
        k = self.bundle["retrieval"]["k"]
        passages = self.f.tools.invoke(ctx, "kb", "search", {"query": question, "k": k, "user": user, "groups": groups})["passages"]
        r = self.agent.run(None, {"kind": "rag.answer", "question": question, "passages": passages},
                           tenant=TENANT, env=self.env, purpose="product.rag", data_class="confidential")
        if r.status != "success":
            return Answer(r.status, "", [], 0.0, None, [], 0)
        review_id = None
        if r.payload["needs_review"]:
            review_id = self.f.tools.invoke(ctx, "review_queue", "submit",
                                            {"question": question, "draft": r.payload["answer"], "reason": "low confidence or no sources"},
                                            op_id=f"hrq:{digest_obj([self.bundle_digest, run_id, user, question])}")["id"]
        denied = [t for t in r.tool_results if not t["ok"]]
        return Answer("pending_review" if review_id else "answered", r.payload["answer"], r.payload["citations"],
                      r.payload["confidence"], review_id, denied, len(r.tool_results))

    def review_queue(self) -> list[dict]:
        return self.f.db.all("SELECT id, ref, summary, status FROM review_items WHERE project=? AND kind='rag_review'", (PROJECT,))

    def resolve_review(self, review_id: str, reviewer: str, decision: str) -> None:
        rv = self.f.directory.get(reviewer)
        if rv.kind != "human":
            raise ToolDenied("review decisions require a human reviewer")
        self.f.db.execute("UPDATE review_items SET status=? WHERE id=?", (decision, review_id))
        self.f.ledger.record("rag.review_resolved", review_id, reviewer, {"decision": decision}, PROJECT)
