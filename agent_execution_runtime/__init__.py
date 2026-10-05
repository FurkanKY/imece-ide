"""Role-free single-agent task execution over the existing runtime ports."""

from agent_execution_runtime.execution import (
    AgentExecutionPorts,
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutionStatus,
    build_agent_ports,
    execute_task,
)
from agent_execution_runtime.lifecycle import AgentRunCoordinator

__all__ = [
    "AgentExecutionPorts", "AgentExecutionRequest", "AgentExecutionResult",
    "AgentExecutionStatus", "build_agent_ports", "execute_task", "AgentRunCoordinator",
]
