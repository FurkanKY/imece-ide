import pytest

import agent_execution_runtime as agent_execution
import engine_factory
from run_runtime.service import RunRuntime


def test_unsupported_provider_is_rejected_before_any_constructor():
    called = []
    with pytest.raises(engine_factory.EngineUnsupportedError):
        agent_execution.build_agent_ports(
            object(), "run", "qwen-code", backend_factory=lambda _provider: called.append("backend"),
            acp_client_factory=lambda: called.append("acp"),
        )
    assert called == []


def test_builds_one_worker_without_planner_or_reviewer(monkeypatch):
    constructed = []
    class Worker:
        pass
    monkeypatch.setattr(engine_factory, "_build_worker", lambda *a, **k: constructed.append("worker") or Worker())
    monkeypatch.setattr(engine_factory, "_build_planner", lambda *a, **k: pytest.fail("planner constructed"))
    monkeypatch.setattr(engine_factory, "_build_reviewer", lambda *a, **k: pytest.fail("reviewer constructed"))
    ports = agent_execution.build_agent_ports(
        RunRuntime.__new__(RunRuntime), "run", "openai", backend_factory=lambda _: object(),
    )
    assert constructed == ["worker"]
    assert isinstance(ports, agent_execution.AgentExecutionPorts)
