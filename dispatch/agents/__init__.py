"""Agent package -- re-exports core types and registers agent implementations."""

from dispatch.agents.base import AgentError, AgentRouter, BaseAgent

# Import to trigger registration
import dispatch.agents.openclaw  # noqa: F401

__all__ = ["AgentError", "AgentRouter", "BaseAgent"]
