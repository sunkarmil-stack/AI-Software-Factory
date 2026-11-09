"""Offline (deterministic) behaviour for each logical agent role.

These handlers stand in for an LLM so that the governed pipeline is fully runnable and
testable without model access. Two handlers deliberately *emulate a model that follows
injected instructions* ("AGENT-INSTRUCTION:" / "SYSTEM OVERRIDE:" lines in source or
retrieved content). This lets the acceptance suite prove that containment comes from the
tool gateway, identity and policy — not from model refusal (REQ 038, ACC 02).
"""
from __future__ import annotations

import json
import re

from ..core.util import digest_obj
from ..models.gateway import offline_handler
from .codegen import generate_service, generate_tests

VAGUE = ["fast", "appropriate", "user-friendly", "as needed", "etc", "reasonable", "robust", "seamless", "quickly"]
INJECT = re.compile(r"(?:AGENT-INSTRUCTION|SYSTEM OVERRIDE):\s*(\{.*\})")


def env(status, summary, payload, refs, **kw):
    return {"status": status, "summary": summary, "payload": payload, "evidence_refs": refs, **kw}


def _injected_requests(texts: list[str]) -> list[dict]:
    reqs = []
    for t in texts:
        for m in INJECT.finditer(t or ""):
            try:
                reqs.append(json.loads(m.group(1)))
            except json.JSONDecodeError:
                pass
    return reqs


@offline_handler("spec.analyze")
def spec_analyze(task):
    spec = task["spec"]
    questions = []
    for r in spec["requirements"]:
        for w in VAGUE:
            if re.search(rf"\b{re.escape(w)}\b", r["statement"], re.I):
                questions.append({"ref": r["id"], "question": f"'{w}' is not measurable; specify a threshold"})
    open_asm = [a["id"] for a in spec.get("assumptions", []) if a["status"] != "validated"]
    for a in open_asm:
        questions.append({"ref": a, "question": "assumption not validated; confirm or schedule a spike"})
    status = "blocked" if questions else "success"
    return env(status, f"{len(questions)} clarification(s) needed" if questions else "specification is testable",
               {"gaps": questions, "requirement_count": len(spec["requirements"]),
                "ac_count": sum(len(r["acceptance_criteria"]) for r in spec["requirements"])},
               [digest_obj(spec)], questions=questions)


@offline_handler("architecture.propose")
def architecture_propose(task):
    spec = task["spec"]
    approved = {d["id"] for d in spec["decisions"] if d["status"] == "approved"}
    proposals = []
    for r in spec["requirements"]:
        for d in r.get("decisions", []):
            if d not in approved:
                proposals.append({"id": d, "context": f"needed by {r['id']}", "options": ["TBD-A", "TBD-B"],
                                  "decision": "unresolved", "status": "proposed"})
    threats = []
    for c in spec["contracts"]:
        for ep in c["endpoints"]:
            name = f"{ep['method']} {ep['path']}"
            threats += [
                {"id": f"TM-{len(threats)+1}", "stride": "Spoofing", "target": name,
                 "mitigation": "gateway token validation + service claim check"},
                {"id": f"TM-{len(threats)+2}", "stride": "Elevation of privilege", "target": name,
                 "mitigation": f"require_scope('{ep['required_scope']}') in handler; SAST rule"},
                {"id": f"TM-{len(threats)+3}", "stride": "Information disclosure", "target": name,
                 "mitigation": "tenant-filtered store access; 404 on foreign tenant"},
            ]
    return env("success", f"{len(threats)} threats, {len(proposals)} decisions to resolve",
               {"threats": threats, "decision_proposals": proposals}, [digest_obj(spec)])


@offline_handler("planning.plan")
def planning_plan(task):
    spec = task["spec"]
    tasks = [{"id": "TASK-1", "title": "Generate service routes, adapter and OpenAPI from contract",
              "requirements": [r["id"] for r in spec["requirements"]], "depends_on": [], "estimate_h": 2,
              "agent": "implementation"},
             {"id": "TASK-2", "title": "Propose unit test candidates from acceptance criteria",
              "requirements": [r["id"] for r in spec["requirements"]], "depends_on": ["TASK-1"], "estimate_h": 1,
              "agent": "test"},
             {"id": "TASK-3", "title": "Run tests in sandbox and open pull request",
              "requirements": [r["id"] for r in spec["requirements"]], "depends_on": ["TASK-1", "TASK-2"],
              "estimate_h": 0.5, "agent": "implementation"}]
    return env("success", f"{len(tasks)} bounded tasks", {"tasks": tasks}, [digest_obj(spec)])


@offline_handler("implementation.generate")
def implementation_generate(task):
    spec, contract = task["spec"], task["contract"]
    files = generate_service(spec, contract)
    if task.get("defect") == "drop_authz":  # used by REQ 036 test: a deliberately broken implementation
        files["app/routes.py"] = files["app/routes.py"].replace(
            '    c = require_scope(req, "accounts:read")\n    return 200, {"items"',
            '    from .security import caller\n    c = caller(req)\n    return 200, {"items"')
    injected = _injected_requests(list((task.get("workspace") or {}).values()))
    return env("success", f"generated {len(files)} files", {"files": files},
               [f"{contract['id']}@{contract['version']}"], tool_requests=injected)


@offline_handler("test.generate")
def test_generate(task):
    src = generate_tests(task["spec"], "Unit test candidates proposed by the test agent", "test_candidate")
    return env("success", "test candidates from acceptance criteria", {"files": {"tests/unit/test_generated.py": src}},
               [ac["id"] for r in task["spec"]["requirements"] for ac in r["acceptance_criteria"]])


@offline_handler("review.diff")
def review_diff(task):
    findings = []
    for path, text in task["files"].items():
        if INJECT.search(text) or re.search(r"(?i)ignore (all )?previous instructions", text):
            findings.append({"severity": "high", "file": path, "finding": "embedded instruction targeting agents (untrusted input)"})
        if re.search(r"(?i)(password|secret|api_key)\s*=\s*['\"][^'\"]{6,}", text):
            findings.append({"severity": "high", "file": path, "finding": "possible hard-coded secret"})
        if path.endswith("routes.py"):
            for m in re.finditer(r"def (\w+)\(req\):\n((?:    .*\n)+)", text):
                if "require_scope" not in m.group(2) and "healthz" not in m.group(1):
                    findings.append({"severity": "critical", "file": path, "finding": f"handler {m.group(1)} lacks authorization"})
    return env("success", f"{len(findings)} finding(s); advisory only — does not approve",
               {"findings": findings, "advisory": True}, [task.get("head_sha", "")])


@offline_handler("release.advise")
def release_advise(task):
    ev = task["evidence"]
    missing = [k for k, v in ev["checks"].items() if v != "passed"]
    rec = "do not promote" if missing or ev.get("stale") else "evidence complete; human authorization required"
    return env("success", rec, {"missing": missing, "recommendation": rec, "can_authorize": False},
               [ev.get("manifest_digest", "")])


@offline_handler("ops.triage")
def ops_triage(task):
    props = []
    for name, rows in task["metrics"].items():
        for row in rows:
            labels = json.loads(row["labels"]) if isinstance(row["labels"], str) else row["labels"]
            cat = labels.get("category")
            if cat and row["value"] >= task.get("threshold", 3):
                props.append({"title": f"Investigate {name} ({cat})", "category": cat, "count": row["value"],
                              "confidence": "medium", "uncertainty": "correlation from aggregate metrics only"})
    return env("success", f"{len(props)} proposal(s); no operational changes made", {"proposals": props},
               ["metrics-snapshot"])


@offline_handler("rag.answer")
def rag_answer(task):
    q = set(re.findall(r"[a-z0-9]+", task["question"].lower()))
    best, cites = [], []
    for p in task["passages"]:
        for sent in re.split(r"(?<=[.!?])\s+", p["text"]):
            overlap = len(q & set(re.findall(r"[a-z0-9]+", sent.lower())))
            if overlap:
                best.append((overlap, sent, p["citation"]))
    cfg = task.get("prompt_config", {})
    best.sort(key=lambda x: -x[0])
    chosen = best[:cfg.get("max_sentences", 2)]
    answer = " ".join(s for _, s, _ in chosen) if chosen else "I could not find this in the approved sources."
    cites = sorted({c for _, _, c in chosen})
    conf = min(1.0, (chosen[0][0] / max(len(q), 1)) if chosen else 0.0)
    injected = (_injected_requests([p["text"] for p in task["passages"]])
                if cfg.get("document_instruction_policy") == "follow" else [])
    return env("success", "answer drafted", {"answer": answer, "citations": cites, "confidence": round(conf, 2),
                                            "needs_review": conf < 0.35 or not cites},
               cites or ["no-sources"], tool_requests=injected)
