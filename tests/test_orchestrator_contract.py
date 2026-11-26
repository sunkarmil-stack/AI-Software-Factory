"""Lifecycle contract for orchestration adapters (REQ 011/012). The native engine is tested
throughout the suite; the LangGraph adapter runs the same contract once langgraph is installed."""
import importlib.util

import pytest

from factory.orchestrator.engine import Step, Workflow


def test_langgraph_adapter_contract():
    if importlib.util.find_spec("langgraph") is None:
        from factory.orchestrator.langgraph_adapter import to_langgraph
        with pytest.raises(RuntimeError, match="not installed"):
            to_langgraph(Workflow("w", "1", [Step("a", lambda c: {})]))
        pytest.skip("langgraph not installed (Phase 0 spike dependency)")
    from factory.orchestrator.langgraph_adapter import to_langgraph
    g = to_langgraph(Workflow("w", "1", [Step("a", lambda c: {"x": 1}), Step("b", lambda c: {"y": 2}, ["a"])]))
    out = g.invoke({"ctx": None})
    assert out["a"] == {"x": 1} and out["b"] == {"y": 2}
