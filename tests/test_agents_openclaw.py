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
    with patch("dispatch.agents.openclaw.load_or_create_key") as mock_key:
        from dispatch.crypto import _generate_keypair
        key = _generate_keypair()
        mock_key.return_value = key
        a = OpenClawAgent(
            name="navi",
            voice="en-US-AvaMultilingualNeural",
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


def _make_ws_mock():
    """Create a fresh AsyncMock WebSocket with challenge/hello-ok sequence."""
    mock = AsyncMock()
    mock.recv = AsyncMock(side_effect=[_make_challenge(), _make_hello_ok()])
    mock.close = AsyncMock()
    mock.__aiter__ = MagicMock(return_value=AsyncMock(
        __anext__=AsyncMock(side_effect=StopAsyncIteration)
    ))
    sent = []

    async def capture_send(data):
        sent.append(json.loads(data))

    mock.send = capture_send
    mock._sent = sent
    return mock


class TestConnect:
    async def test_connect_performs_dual_handshake(self, agent):
        """connect() should complete both operator and node handshakes."""
        operator_ws = _make_ws_mock()
        node_ws = _make_ws_mock()
        ws_mocks = iter([operator_ws, node_ws])

        async def fake_connect(uri):
            return next(ws_mocks)

        with patch("dispatch.agents.openclaw.websockets.connect", side_effect=fake_connect), \
             patch.object(agent._http, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())
            await agent.connect()

        # Operator connect request
        assert len(operator_ws._sent) == 1
        op_req = operator_ws._sent[0]
        assert op_req["method"] == "connect"
        assert op_req["params"]["role"] == "operator"
        assert op_req["params"]["scopes"] == ["operator.read", "operator.write"]
        assert op_req["params"]["caps"] == []
        assert op_req["params"]["auth"]["token"] == "test-secret-token"
        assert op_req["params"]["device"]["nonce"] == "test-nonce-123"

        # Node connect request
        assert len(node_ws._sent) == 1
        node_req = node_ws._sent[0]
        assert node_req["method"] == "connect"
        assert node_req["params"]["role"] == "node"
        assert node_req["params"]["caps"] == ["voice"]
        assert node_req["params"]["commands"] == ["voice.speak"]
        assert node_req["params"]["permissions"] == {"voice.speak": True}
        assert node_req["params"]["scopes"] == []

        await agent.disconnect()

    async def test_connect_healthz_unreachable_no_raise(self, agent):
        """If healthz fails, connect should not raise (degraded mode)."""
        with patch.object(agent._http, "get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = Exception("connection refused")
            await agent.connect()

        assert agent._ws is None
        assert agent._node_ws is None

    async def test_connect_bad_challenge_degrades(self, agent):
        """If gateway sends unexpected first frame, both connections degrade."""
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(return_value=json.dumps({"type": "event", "event": "unknown"}))
        mock_ws.close = AsyncMock()

        async def fake_connect(uri):
            return mock_ws

        with patch("dispatch.agents.openclaw.websockets.connect", side_effect=fake_connect), \
             patch.object(agent._http, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())
            await agent.connect()

        assert agent._ws is None

    async def test_node_failure_does_not_block_operator(self, agent):
        """If node handshake fails, operator should still work."""
        operator_ws = _make_ws_mock()
        node_ws = AsyncMock()
        node_ws.recv = AsyncMock(return_value=json.dumps({"type": "event", "event": "unknown"}))
        node_ws.close = AsyncMock()
        ws_mocks = iter([operator_ws, node_ws])

        async def fake_connect(uri):
            return next(ws_mocks)

        with patch("dispatch.agents.openclaw.websockets.connect", side_effect=fake_connect), \
             patch.object(agent._http, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())
            await agent.connect()

        # Operator handshake completed successfully
        assert len(operator_ws._sent) == 1
        assert operator_ws._sent[0]["params"]["role"] == "operator"
        assert agent._recv_task is not None
        # Node handshake failed gracefully
        assert agent._node_ws is None

        await agent.disconnect()


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

        with patch("dispatch.agents.openclaw.asyncio.wait_for", side_effect=asyncio.TimeoutError):
            with pytest.raises(AgentError, match="timed out"):
                await agent.send("hi")


class TestDisconnect:
    async def test_disconnect_closes_both_connections(self, agent):
        """disconnect() must close both operator and node WebSockets."""
        op_ws = AsyncMock()
        op_ws.close = AsyncMock()
        node_ws = AsyncMock()
        node_ws.close = AsyncMock()
        agent._ws = op_ws
        agent._node_ws = node_ws
        agent._http.aclose = AsyncMock()

        await agent.disconnect()

        op_ws.close.assert_called_once()
        node_ws.close.assert_called_once()
        agent._http.aclose.assert_called_once()
        assert agent._ws is None
        assert agent._node_ws is None

    async def test_disconnect_without_ws(self, agent):
        """disconnect() should work even if WebSockets were never opened."""
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

    async def test_unrequested_chat_final_routes_to_notification(self, agent):
        """Unrequested chat final events should be routed to the notification queue."""
        queue = NotificationQueue()
        agent._notification_queue = queue

        await agent._handle_event({
            "type": "event",
            "event": "chat",
            "payload": {
                "runId": "unknown-run-999",
                "state": "final",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "Hey, just checking in!"}]},
            },
        })

        notif = queue.get_nowait()
        assert notif.text == "Hey, just checking in!"
        assert notif.agent_name == "navi"
        assert notif.priority == 1

    async def test_unrequested_empty_text_ignored(self, agent):
        """Unrequested chat final with empty text should not enqueue."""
        queue = NotificationQueue()
        agent._notification_queue = queue

        await agent._handle_event({
            "type": "event",
            "event": "chat",
            "payload": {
                "runId": "unknown-run-888",
                "state": "final",
                "message": {"role": "assistant", "content": []},
            },
        })

        assert queue.empty()

    async def test_handle_response_error(self, agent):
        """_handle_response should set exception on error."""
        req_id = "test-req-456"
        fut = asyncio.get_running_loop().create_future()
        agent._pending[req_id] = fut

        agent._handle_response({
            "type": "res",
            "id": req_id,
            "ok": False,
            "error": {"code": "BAD_REQUEST", "message": "bad request"},
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


class TestNodeInvoke:
    async def test_invoke_voice_speak_queues_notification(self, agent):
        """voice.speak invoke should queue an urgent notification and ack."""
        queue = NotificationQueue()
        agent._notification_queue = queue
        agent._node_ws = AsyncMock()

        sent = []

        async def capture(data):
            sent.append(json.loads(data))

        agent._node_ws.send = capture

        await agent._handle_invoke({
            "type": "req",
            "id": "invoke-123",
            "method": "invoke",
            "params": {"command": "voice.speak", "args": {"text": "Time for lunch!"}},
        })

        notif = queue.get_nowait()
        assert notif.text == "Time for lunch!"
        assert notif.priority == 0
        assert notif.agent_name == "navi"

        assert len(sent) == 1
        assert sent[0]["ok"] is True
        assert sent[0]["id"] == "invoke-123"

    async def test_invoke_unknown_command_returns_error(self, agent):
        """Unknown invoke commands should return an error response."""
        agent._node_ws = AsyncMock()
        sent = []

        async def capture(data):
            sent.append(json.loads(data))

        agent._node_ws.send = capture

        await agent._handle_invoke({
            "type": "req",
            "id": "invoke-456",
            "method": "invoke",
            "params": {"command": "camera.snap", "args": {}},
        })

        assert len(sent) == 1
        assert sent[0]["ok"] is False
        assert sent[0]["error"]["code"] == "UNKNOWN_COMMAND"

    async def test_invoke_voice_speak_without_queue_still_acks(self, agent):
        """voice.speak without a notification queue should still ack."""
        agent._node_ws = AsyncMock()
        sent = []

        async def capture(data):
            sent.append(json.loads(data))

        agent._node_ws.send = capture

        await agent._handle_invoke({
            "type": "req",
            "id": "invoke-789",
            "method": "invoke",
            "params": {"command": "voice.speak", "args": {"text": "Hello"}},
        })

        assert len(sent) == 1
        assert sent[0]["ok"] is True

    async def test_invoke_voice_speak_empty_text(self, agent):
        """voice.speak with empty text should ack but not queue notification."""
        queue = NotificationQueue()
        agent._notification_queue = queue
        agent._node_ws = AsyncMock()

        sent = []

        async def capture(data):
            sent.append(json.loads(data))

        agent._node_ws.send = capture

        await agent._handle_invoke({
            "type": "req",
            "id": "invoke-000",
            "method": "invoke",
            "params": {"command": "voice.speak", "args": {"text": ""}},
        })

        assert queue.empty()
        assert len(sent) == 1
        assert sent[0]["ok"] is True
