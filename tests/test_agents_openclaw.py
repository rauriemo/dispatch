"""Tests for dispatch.agents.openclaw -- OpenClawAgent HTTP contract."""

import pytest
from unittest.mock import AsyncMock, patch

import httpx

from dispatch.agents.base import AgentError
from dispatch.agents.openclaw import OpenClawAgent


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setenv("OPENCLAW_TOKEN", "test-secret-token")
    return OpenClawAgent(
        name="navi",
        voice="en-US-GuyNeural",
        endpoint="http://localhost:18789",
        token_env="OPENCLAW_TOKEN",
    )


class TestSend:
    async def test_send_request_format(self, agent, httpx_mock):
        """Verify POST /v1/responses with correct body and auth header."""
        httpx_mock.add_response(
            url="http://localhost:18789/v1/responses",
            method="POST",
            json={"output": [{"content": [{"text": "response"}]}]},
        )

        await agent.send("hello")

        request = httpx_mock.get_request()
        assert request.method == "POST"
        assert str(request.url) == "http://localhost:18789/v1/responses"
        assert request.headers["authorization"] == "Bearer test-secret-token"

        import json
        body = json.loads(request.content)
        assert body == {"model": "openclaw", "input": "hello"}

    async def test_send_parses_response(self, agent, httpx_mock):
        """Verify send() extracts text from output[].content[].text."""
        httpx_mock.add_response(
            url="http://localhost:18789/v1/responses",
            method="POST",
            json={
                "output": [
                    {
                        "content": [
                            {"text": "The answer is 42."}
                        ]
                    }
                ]
            },
        )

        result = await agent.send("what is the answer?")
        assert result == "The answer is 42."

    async def test_send_raises_agent_error_on_timeout(self, agent, httpx_mock):
        """Timeout should raise AgentError."""
        httpx_mock.add_exception(
            httpx.ReadTimeout("timed out"),
            url="http://localhost:18789/v1/responses",
        )

        with pytest.raises(AgentError):
            await agent.send("hello")

    async def test_send_raises_agent_error_on_network_error(self, agent, httpx_mock):
        """Connection error should raise AgentError."""
        httpx_mock.add_exception(
            httpx.ConnectError("connection refused"),
            url="http://localhost:18789/v1/responses",
        )

        with pytest.raises(AgentError):
            await agent.send("hello")

    async def test_send_raises_agent_error_on_http_error(self, agent, httpx_mock):
        """500 response should raise AgentError."""
        httpx_mock.add_response(
            url="http://localhost:18789/v1/responses",
            method="POST",
            status_code=500,
        )

        with pytest.raises(AgentError):
            await agent.send("hello")


class TestConnect:
    async def test_connect_healthz_success(self, agent, httpx_mock):
        """GET /healthz returning 200 should not raise."""
        httpx_mock.add_response(
            url="http://localhost:18789/healthz",
            method="GET",
            status_code=200,
        )

        # Should not raise
        await agent.connect()

    async def test_connect_healthz_unreachable(self, agent, httpx_mock):
        """Connection refused on /healthz should log warning but NOT raise."""
        httpx_mock.add_exception(
            httpx.ConnectError("connection refused"),
            url="http://localhost:18789/healthz",
        )

        # Should not raise -- logs warning, agent continues degraded
        await agent.connect()


class TestDisconnect:
    async def test_disconnect_closes_client(self, agent):
        """disconnect() must call aclose() on the httpx client."""
        agent._client.aclose = AsyncMock()

        await agent.disconnect()

        agent._client.aclose.assert_called_once()
