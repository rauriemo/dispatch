"""Tests for dispatch.agents.base -- AgentRouter, BaseAgent, AgentError."""

from unittest.mock import AsyncMock

import pytest

from dispatch.agents.base import AgentError, AgentRouter
from dispatch.agents.openclaw import OpenClawAgent
from dispatch.config import AgentConfig


class TestAgentError:
    def test_agent_error_is_exception(self):
        """AgentError must be a subclass of Exception."""
        assert issubclass(AgentError, Exception)
        err = AgentError("test")
        assert isinstance(err, Exception)
        assert str(err) == "test"


class TestAgentRouter:
    def test_router_creates_correct_agent_type(self, sample_agent_config):
        """Given config with type: openclaw, router instantiates OpenClawAgent."""
        router = AgentRouter([sample_agent_config])
        assert len(router.agents) == 1
        assert isinstance(router.agents[0], OpenClawAgent)

    def test_router_route_returns_correct_agent(self, sample_agent_config):
        """Given 2 agents, route(0) and route(1) return the correct ones."""
        config2 = AgentConfig(
            name="jarvis",
            type="openclaw",
            wake_word="assets/hey-jarvis.ppn",
            endpoint="http://localhost:9999",
            token_env="JARVIS_TOKEN",
            voice="en-US-AriaNeural",
        )
        router = AgentRouter([sample_agent_config, config2])
        assert router.route(0).name == "navi"
        assert router.route(1).name == "jarvis"

    def test_router_route_invalid_index_raises(self, sample_agent_config):
        """route(99) should raise IndexError."""
        router = AgentRouter([sample_agent_config])
        with pytest.raises(IndexError):
            router.route(99)

    def test_router_keyword_paths(self, sample_agent_config):
        """ppn_paths returns paths in same order as agents."""
        config2 = AgentConfig(
            name="jarvis",
            type="openclaw",
            wake_word="assets/hey-jarvis.ppn",
            endpoint="http://localhost:9999",
            token_env="JARVIS_TOKEN",
            voice="en-US-AriaNeural",
        )
        router = AgentRouter([sample_agent_config, config2])
        paths = router.ppn_paths
        assert len(paths) == 2
        assert paths[0].endswith("hey-navi.ppn")
        assert paths[1].endswith("hey-jarvis.ppn")

    @pytest.mark.asyncio
    async def test_router_context_manager_connects_and_disconnects(self, sample_agent_config, monkeypatch):
        """async with AgentRouter should call connect() on enter and disconnect() on exit."""
        router = AgentRouter([sample_agent_config])
        agent = router.agents[0]
        agent.connect = AsyncMock()
        agent.disconnect = AsyncMock()

        async with router:
            agent.connect.assert_called_once()

        agent.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_router_degraded_agent_on_connect_failure(self, sample_agent_config):
        """If connect() raises, router logs warning and still starts."""
        router = AgentRouter([sample_agent_config])
        agent = router.agents[0]
        agent.connect = AsyncMock(side_effect=ConnectionError("refused"))
        agent.disconnect = AsyncMock()

        # Should not raise
        async with router as r:
            assert len(r.agents) == 1
            assert r.agents[0].name == "navi"

    def test_router_unknown_type_skipped(self):
        """Unknown agent type should be skipped with no crash."""
        bad_config = AgentConfig(
            name="ghost",
            type="nonexistent",
            wake_word="assets/ghost.ppn",
            endpoint="http://localhost:0",
            token_env="GHOST_TOKEN",
            voice="en-US-GuyNeural",
        )
        router = AgentRouter([bad_config])
        assert len(router.agents) == 0
