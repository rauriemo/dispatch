"""Tests for dispatch.agents.openclaw -- WebSocket gateway protocol."""

import asyncio
import json
import uuid

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from dispatch.agents.base import AgentError
from dispatch.agents.openclaw import OpenClawAgent
from dispatch.notifications import NotificationQueue


@pytest.fixture
def agent(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENCLAW_TOKEN", "test-secret-token")
    key_path = tmp_path / "test_device_key"
    with patch("dispatch.agents.openclaw.load_or_create_key") as mock_key:
        from dispatch.crypto import _generate_keypair
        key = _generate_keypair()
        mock_key.return_value = key
        a = OpenClawAgent(
            name="navi",
            voice="en-US-GuyNeural",
            endpoint="http://localhost:18789",
            token_env="OPENCLAW_TOKEN",
        )
    return a


def _make_challenge(nonce="test-nonce-123", ts=1000000):
    return json.dumps({
        "type": "event",
        "event": "connect.challenge",
        "payload": {"nonce": nonce, "ts": ts},
    })


def _make_hello_ok(req_id="ignored"):
    return json.dumps({
        "type": "res",
        "id": req_id,
        "ok": True,
        "payload": {"type": "hello-ok", "protocol": 3, "policy": {"tickIntervalMs": 15000}},
    })


class TestConnect:
    async def test_connect_performs_handshake(self, agent):
        """connect() should complete the gateway handshake."""
        mock_ws = AsyncMock()
        sent_messages = []

        async def capture_send(data):
            sent_messages.append(json.loads(data))

        mock_ws.send = capture_send
        mock_ws.recv = AsyncMock(side_effect=[_make_challenge(), _make_hello_ok()])
        mock_ws.close = AsyncMock()
        # Make the recv loop end immediately
        mock_ws.__aiter__ = MagicMock(return_value=AsyncMock(
            __anext__=AsyncMock(side_effect=StopAsyncIteration)
        ))

        async def fake_connect(uri):
            return mock_ws

        with patch("dispatch.agents.openclaw.websockets.connect", side_effect=fake_connect), \
             patch.object(agent._http, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())
            await agent.connect()

        # Verify connect request was sent
        assert len(sent_messages) == 1
        req = sent_messages[0]
        assert req["type"] == "req"
        assert req["method"] == "connect"
        assert req["params"]["role"] == "operator"
        assert req["params"]["scopes"] == ["operator.read", "operator.write"]
        assert req["params"]["auth"]["token"] == "test-secret-token"
        assert req["params"]["device"]["nonce"] == "test-nonce-123"

        # Cleanup
        await agent.disconnect()

    async def test_connect_healthz_unreachable_no_raise(self, agent):
        """If healthz fails, connect should not raise (degraded mode)."""
        with patch.object(agent._http, "get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = Exception("connection refused")
            await agent.connect()

        assert agent._ws is None

    async def test_connect_bad_challenge_raises(self, agent):
        """If gateway sends unexpected first frame, connect degrades."""
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(return_value=json.dumps({"type": "event", "event": "unknown"}))
        mock_ws.close = AsyncMock()

        async def fake_connect(uri):
            return mock_ws

        with patch("dispatch.agents.openclaw.websockets.connect", side_effect=fake_connect), \
             patch.object(agent._http, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())
            await agent.connect()

        # Should have fallen back to degraded (ws = None)
        assert agent._ws is None


class TestSend:
    async def test_send_raises_when_not_connected(self, agent):
        """send() should raise AgentError if WebSocket is not connected."""
        with pytest.raises(AgentError, match="not connected"):
            await agent.send("hello")

    async def test_send_returns_response_text(self, agent):
        """send() should collect delta events and return the final text."""
        mock_ws = AsyncMock()
        agent._ws = mock_ws

        sent_messages = []

        async def capture_send(data):
            msg = json.loads(data)
            sent_messages.append(msg)
            req_id = msg["id"]

            # Simulate response events arriving via the recv loop
            # We need to resolve the future directly since recv loop is mocked
            fut = agent._pending.get(req_id)
            if fut:
                fut._text_buf = ["Hello ", "world!"]
                fut.set_result("Hello world!")

        mock_ws.send = capture_send

        result = await agent.send("hi")
        assert result == "Hello world!"
        assert len(sent_messages) == 1
        assert sent_messages[0]["method"] == "chat.send"
        assert sent_messages[0]["params"]["message"] == "hi"
        assert "sessionKey" in sent_messages[0]["params"]
        assert "idempotencyKey" in sent_messages[0]["params"]

    async def test_send_timeout_raises_agent_error(self, agent):
        """send() should raise AgentError on timeout."""
        mock_ws = AsyncMock()
        agent._ws = mock_ws
        mock_ws.send = AsyncMock()

        # Patch the internal timeout to be very short
        with patch("dispatch.agents.openclaw.asyncio.wait_for", side_effect=asyncio.TimeoutError):
            with pytest.raises(AgentError, match="timed out"):
                await agent.send("hi")


class TestDisconnect:
    async def test_disconnect_closes_ws_and_http(self, agent):
        """disconnect() must close WebSocket and HTTP client."""
        mock_ws = AsyncMock()
        mock_ws.close = AsyncMock()
        agent._ws = mock_ws
        agent._http.aclose = AsyncMock()

        await agent.disconnect()

        mock_ws.close.assert_called_once()
        agent._http.aclose.assert_called_once()
        assert agent._ws is None

    async def test_disconnect_without_ws(self, agent):
        """disconnect() should work even if WebSocket was never opened."""
        agent._http.aclose = AsyncMock()
        await agent.disconnect()
        agent._http.aclose.assert_called_once()


class TestRecvLoop:
    async def test_handle_event_delta_and_final(self, agent):
        """_handle_event should accumulate deltas and resolve on chat final."""
        run_id = "test-run-123"
        fut = asyncio.get_running_loop().create_future()
        agent._pending[run_id] = fut

        await agent._handle_event({
            "type": "event",
            "event": "agent",
            "payload": {"runId": run_id, "stream": "assistant", "data": {"delta": "Hello "}},
        })
        await agent._handle_event({
            "type": "event",
            "event": "agent",
            "payload": {"runId": run_id, "stream": "assistant", "data": {"delta": "world!"}},
        })
        await agent._handle_event({
            "type": "event",
            "event": "chat",
            "payload": {
                "runId": run_id,
                "state": "final",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello world!"}]},
            },
        })

        result = await fut
        assert result == "Hello world!"

    async def test_handle_response_error(self, agent):
        """_handle_response should set exception on error."""
        req_id = "test-req-456"
        fut = asyncio.get_event_loop().create_future()
        agent._pending[req_id] = fut

        agent._handle_response({
            "type": "res",
            "id": req_id,
            "ok": False,
            "payload": {"error": "bad request"},
        })

        with pytest.raises(AgentError):
            await fut

    async def test_notification_event(self, agent):
        """notification events should be routed to the notification queue."""
        queue = NotificationQueue()
        agent._notification_queue = queue

        await agent._handle_notification({"text": "Alert!", "urgent": True})

        notif = queue.get_nowait()
        assert notif.text == "Alert!"
        assert notif.priority == 0
        assert notif.agent_name == "navi"
