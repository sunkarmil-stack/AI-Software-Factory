"""Context & retrieval service (REQ 024-027, ACC 06).

- Authorization filters (tenant, project, user/group ACL, purpose, deletion) run BEFORE
  scoring, so denied content can never reach a model request.
- Structure-aware chunking (markdown headings -> paragraphs), hybrid lexical retrieval
  (BM25 + TF-IDF cosine fused with reciprocal rank fusion), citations with source identity,
  version and provenance.
- Revocation/deletion bump a generation counter; caches are partitioned by
  (tenant, project, ACL fingerprint) and invalidated by generation. If freshness cannot be
  established for sensitive material the service fails closed.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from ..core.db import DB, j, unj
from ..core.evidence import Ledger
from ..core.util import digest_obj, now

TOKEN = re.compile(r"[a-z0-9]+")
STOP = set("the a an of to and or in on for is are be with by as at it this that from can not".split())
SENSITIVE = {"confidential", "restricted"}


def toks(s: str) -> list[str]:
    return [t for t in TOKEN.findall(s.lower()) if t not in STOP]


@dataclass
class Chunk:
    doc_id: str
    version: int
    tenant: str
    project: str
    acl: list[str]
    classification: str
    title: str
    heading: str
    text: str
    source: str
    tokens: list[str] = field(default_factory=list)


@dataclass
class Requester:
    tenant: str
    project: str
    user: str
    groups: list[str]
    purpose: str
    allowed_purposes: tuple = ("product.rag", "delivery.context")


def chunk_markdown(text: str, max_words: int = 120) -> list[tuple[str, str]]:
    out, heading = [], ""
    for block in re.split(r"\n\s*\n", text):
        b = block.strip()
        if not b:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", b)
        if m and "\n" not in b:
            heading = m.group(2); continue
        if m:
            heading = m.group(2); b = b.split("\n", 1)[1]
        words = b.split()
        for i in range(0, len(words), max_words):
            out.append((heading, " ".join(words[i:i + max_words])))
    return out


class RetrievalService:
    def __init__(self, db: DB, ledger: Ledger, freshness_s: float = 60.0):
        self.db, self.ledger, self.freshness_s = db, ledger, freshness_s
        self._chunks: list[Chunk] = []
        self._built_gen = -1.0
        self._built_at = 0.0
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self.index_down = False  # fault injection

    # ------------- ingestion / lifecycle -------------
    def _gen(self) -> float:
        r = self.db.one("SELECT COALESCE(MAX(updated),0) g, COUNT(*) n FROM docs")
        return r["g"] + r["n"] * 1e-6

    def ingest(self, doc_id: str, tenant: str, project: str, acl: list[str], classification: str, title: str,
               text: str, source: str, actor: str = "ingest") -> int:
        r = self.db.one("SELECT MAX(version) v FROM docs WHERE id=?", (doc_id,))
        v = (r["v"] or 0) + 1
        self.db.execute("UPDATE docs SET deleted=1 WHERE id=?", (doc_id,))
        self.db.execute("INSERT INTO docs(id,version,tenant,project,acl,classification,title,text,source,deleted,updated)"
                        " VALUES(?,?,?,?,?,?,?,?,?,0,?)",
                        (doc_id, v, tenant, project, j(acl), classification, title, text, source, now()))
        self.ledger.record("context.ingested", f"{doc_id}@v{v}", actor,
                           {"tenant": tenant, "acl": acl, "digest": digest_obj(text), "source": source}, project)
        return v

    def revoke(self, doc_id: str, principal_or_group: str, actor: str) -> None:
        r = self.db.one("SELECT * FROM docs WHERE id=? AND deleted=0", (doc_id,))
        acl = [a for a in unj(r["acl"]) if a != principal_or_group]
        self.ingest(doc_id, r["tenant"], r["project"], acl, r["classification"], r["title"], r["text"], r["source"], actor)

    def delete(self, doc_id: str, actor: str) -> None:
        self.db.execute("UPDATE docs SET deleted=1, updated=? WHERE id=?", (now(), doc_id))
        self.ledger.record("context.deleted", doc_id, actor, {})

    def _build(self) -> None:
        if self.index_down:
            return
        rows = self.db.all("SELECT * FROM docs WHERE deleted=0")
        chunks = []
        for r in rows:
            for heading, text in chunk_markdown(r["text"]):
                chunks.append(Chunk(r["id"], r["version"], r["tenant"], r["project"], unj(r["acl"]),
                                    r["classification"], r["title"], heading, text, r["source"],
                                    toks(f"{r['title']} {heading} {text}")))
        self._chunks, self._built_gen, self._built_at = chunks, self._gen(), now()
        self._cache.clear()

    def _ensure_fresh(self, sensitive_possible: bool) -> None:
        g = self._gen()
        if g != self._built_gen:
            self._build()
        if self._built_gen != g:
            # could not rebuild: index is stale relative to permission/deletion changes
            if sensitive_possible or now() - self._built_at > self.freshness_s:
                raise PermissionError("retrieval freshness cannot be established; failing closed")

    # ------------- search -------------
    def _allowed(self, c: Chunk, rq: Requester) -> bool:
        return (c.tenant == rq.tenant and c.project == rq.project
                and (rq.user in c.acl or any(g in c.acl for g in rq.groups) or "public" in c.acl))

    def search(self, rq: Requester, query: str, k: int = 4) -> list[dict]:
        if rq.purpose not in rq.allowed_purposes:
            raise PermissionError(f"purpose {rq.purpose} not permitted for retrieval")
        self._ensure_fresh(sensitive_possible=True)
        acl_fp = digest_obj(sorted([rq.user] + rq.groups))
        ck = digest_obj([rq.tenant, rq.project, acl_fp, query, k])
        hit = self._cache.get(ck)
        if hit and hit[0] == self._built_gen:
            return hit[1]
        cand = [c for c in self._chunks if self._allowed(c, rq)]  # authorization BEFORE scoring
        if not cand:
            return []
        q = toks(query)
        # BM25
        N = len(cand)
        avgdl = sum(len(c.tokens) for c in cand) / N
        df = Counter(t for c in cand for t in set(c.tokens))
        def bm25(c: Chunk) -> float:
            tf = Counter(c.tokens); s = 0.0
            for t in q:
                if t in tf:
                    idf = math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5))
                    s += idf * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * len(c.tokens) / avgdl))
            return s
        # TF-IDF cosine
        def tfidf_vec(ts: list[str]) -> dict:
            tf = Counter(ts)
            return {t: (1 + math.log(n)) * math.log(1 + N / (1 + df.get(t, 0))) for t, n in tf.items()}
        qv = tfidf_vec(q)
        qn = math.sqrt(sum(v * v for v in qv.values())) or 1
        def cos(c: Chunk) -> float:
            v = tfidf_vec(c.tokens); n = math.sqrt(sum(x * x for x in v.values())) or 1
            return sum(qv[t] * v.get(t, 0) for t in qv) / (qn * n)
        r1 = sorted(range(N), key=lambda i: -bm25(cand[i]))
        r2 = sorted(range(N), key=lambda i: -cos(cand[i]))
        fused: Counter = Counter()
        for rank, i in enumerate(r1):
            fused[i] += 1 / (60 + rank)
        for rank, i in enumerate(r2):
            fused[i] += 1 / (60 + rank)
        top = [i for i, _ in fused.most_common(k) if bm25(cand[i]) > 0 or cos(cand[i]) > 0]
        out = [{"doc_id": cand[i].doc_id, "version": cand[i].version, "title": cand[i].title,
                "heading": cand[i].heading, "text": cand[i].text, "source": cand[i].source,
                "classification": cand[i].classification, "score": round(fused[i], 5),
                "citation": f"[{cand[i].doc_id}@v{cand[i].version}#{cand[i].heading}]"} for i in top]
        self._cache[ck] = (self._built_gen, out)
        self.ledger.record("context.retrieved", rq.user, "retrieval-service",
                           {"tenant": rq.tenant, "purpose": rq.purpose, "query_digest": digest_obj(query),
                            "refs": [o["citation"] for o in out]}, rq.project)
        return out


def retrieval_metrics(results: list[list[dict]], relevant: list[set[str]], k: int) -> dict:
    recall, mrr = [], []
    for res, rel in zip(results, relevant):
        ids = [r["doc_id"] for r in res[:k]]
        recall.append(len(rel & set(ids)) / len(rel) if rel else 1.0)
        rr = 0.0
        for i, d in enumerate(ids):
            if d in rel:
                rr = 1 / (i + 1); break
        mrr.append(rr)
    return {"recall_at_k": sum(recall) / len(recall), "mrr": sum(mrr) / len(mrr), "n": len(results)}
