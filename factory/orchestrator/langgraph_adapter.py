"""LangGraph adapter (REQ 011/012 — expansion; Phase 0 spike target).

Maps a factory Workflow onto a LangGraph StateGraph so the spike can compare durable
state, approval waits (interrupts) and failure behaviour against the native engine using
the same lifecycle contract tests (tests/test_orchestrator_contract.py).

The factory remains the single accountable parent: LangGraph executes node bodies, but
run identity, limits, approvals and the side-effect operation ids stay with the factory
Engine. LangGraph checkpointing does not provide exactly-once external effects [S3]; the
tool gateway's idempotency/reconciliation still applies.

LangGraph is not installed in this reference environment; importing this module without
it raises a clear error and the contract tests skip.
"""
from __future__ import annotations

from .engine import Workflow


def to_langgraph(wf: Workflow, checkpointer=None):
    try:
        from langgraph.graph import StateGraph, START, END  # type: ignore
        from langgraph.types import interrupt  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("langgraph is not installed; pip install langgraph to run the spike") from e

    g = StateGraph(dict)
    for s in wf.steps:
        def node(state, _s=s):
            if _s.kind == "approval":
                decision = interrupt({"step": _s.id, "request": _s.approval(state["ctx"])})
                return {**state, _s.id: decision}
            return {**state, _s.id: _s.fn(state["ctx"])}
        g.add_node(s.id, node)
    for s in wf.steps:
        if not s.deps:
            g.add_edge(START, s.id)
        for d in s.deps:
            g.add_edge(d, s.id)
    terminals = {s.id for s in wf.steps} - {d for s in wf.steps for d in s.deps}
    for t in terminals:
        g.add_edge(t, END)
    return g.compile(checkpointer=checkpointer)
