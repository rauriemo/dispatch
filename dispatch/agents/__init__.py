"""Agent package -- re-exports core types and registers agent implementations."""

# Import to trigger registration
import dispatch.agents.anthem  # noqa: F401
import dispatch.agents.openclaw  # noqa: F401
from dispatch.agents.base import AgentError, AgentRouter, BaseAgent

__all__ = ["AgentError", "AgentRouter", "BaseAgent"]
