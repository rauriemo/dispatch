"""Tests for dispatch.agents.anthem -- WebSocket protocol for Anthem orchestrator."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from dispatch.agents.anthem import AnthemAgent
from dispatch.agents.base import AgentError
from dispatch.notifications import NotificationQueue


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setenv("ANTHEM_TOKEN", "test-anthem-token")
    return AnthemAgent(
        name="anthem",
        voice="google/en-US-Chirp3-HD-Erinome",
        endpoint="ws://localhost:8081",
        token_env="ANTHEM_TOKEN",
    )


def _make_ws_mock(auth_response=None):
    """Create a mock WebSocket that completes auth handshake."""
    mock = AsyncMock()
    if auth_response is None:
        auth_response = {"type": "auth_ok"}
    mock.recv = AsyncMock(return_value=json.dumps(auth_response))
    mock.close = AsyncMock()
    mock.__aiter__ = MagicMock(return_value=AsyncMock(__anext__=AsyncMock(side_effect=StopAsyncIteration)))
    sent = []

    async def capture_send(data):
        sent.append(json.loads(data))

    mock.send = capture_send
    mock._sent = sent
    return mock


# -- URI parsing ---------------------------------------------------------------


class TestURIParsing:
    def test_ws_uri_passthrough(self, agent):
        assert agent._ws_uri == "ws://localhost:8081"

    def test_http_to_ws(self, monkeypatch):
        monkeypatch.setenv("ANTHEM_TOKEN", "t")
        a = AnthemAgent("a", "v", "http://localhost:8081", "ANTHEM_TOKEN")
        assert a._ws_uri == "ws://localhost:8081"

    def test_https_to_wss(self, monkeypatch):
        monkeypatch.setenv("ANTHEM_TOKEN", "t")
        a = AnthemAgent("a", "v", "https://anthem.example.com", "ANTHEM_TOKEN")
        assert a._ws_uri == "wss://anthem.example.com"

    def test_trailing_slash_stripped(self, monkeypatch):
        monkeypatch.setenv("ANTHEM_TOKEN", "t")
        a = AnthemAgent("a", "v", "ws://localhost:8081/", "ANTHEM_TOKEN")
        assert a._ws_uri == "ws://localhost:8081"


# -- Connect -------------------------------------------------------------------


class TestConnect:
    async def test_connect_sends_auth_and_starts_recv(self, agent):
        mock_ws = _make_ws_mock()

        with patch("dispatch.agents.anthem.websockets.connect", AsyncMock(return_value=mock_ws)):
            await agent.connect()

        assert len(mock_ws._sent) == 1
        auth_msg = mock_ws._sent[0]
        assert auth_msg["type"] == "auth"
        assert auth_msg["token"] == "test-anthem-token"
        assert auth_msg["client"] == "dispatch"
        assert agent._ws is mock_ws
        assert agent._recv_task is not None

        await agent.disconnect()

    async def test_connect_auth_fail_degrades(self, agent):
        mock_ws = _make_ws_mock({"type": "auth_fail", "error": "invalid token"})

        with patch("dispatch.agents.anthem.websockets.connect", AsyncMock(return_value=mock_ws)):
            await agent.connect()

        assert agent._ws is None
        assert agent._recv_task is not None
        await agent.disconnect()

    async def test_connect_network_error_degrades(self, agent):
        with patch(
            "dispatch.agents.anthem.websockets.connect",
            AsyncMock(side_effect=OSError("refused")),
        ):
            await agent.connect()

        assert agent._ws is None
        assert agent._recv_task is not None
        await agent.disconnect()

    async def test_connect_auth_timeout_degrades(self, agent):
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(side_effect=asyncio.TimeoutError)
        mock_ws.close = AsyncMock()
        mock_ws.send = AsyncMock()

        with patch("dispatch.agents.anthem.websockets.connect", AsyncMock(return_value=mock_ws)):
            await agent.connect()

        assert agent._ws is None
        assert agent._recv_task is not None
        await agent.disconnect()


# -- Send ----------------------------------------------------------------------


class TestSend:
    async def test_send_raises_when_not_connected(self, agent):
        with pytest.raises(AgentError, match="not connected"):
            await agent.send("hello")

    async def test_send_returns_response_text(self, agent):
        mock_ws = AsyncMock()
        agent._ws = mock_ws

        sent_messages = []

        async def capture_send(data):
            msg = json.loads(data)
            sent_messages.append(msg)
            req_id = msg["id"]
            fut = agent._pending.get(req_id)
            if fut and not fut.done():
                fut.set_result("I'll dispatch that task now.")

        mock_ws.send = capture_send

        result = await agent.send("deploy staging")
        assert result == "I'll dispatch that task now."
        assert len(sent_messages) == 1
        assert sent_messages[0]["type"] == "req"
        assert sent_messages[0]["text"] == "deploy staging"
        assert "id" in sent_messages[0]

    async def test_send_timeout_raises_agent_error(self, agent):
        mock_ws = AsyncMock()
        agent._ws = mock_ws
        mock_ws.send = AsyncMock()

        with patch("dispatch.agents.anthem.asyncio.wait_for", side_effect=asyncio.TimeoutError):
            with pytest.raises(AgentError, match="timed out"):
                await agent.send("hello")

    async def test_send_cleans_pending_on_timeout(self, agent):
        mock_ws = AsyncMock()
        agent._ws = mock_ws
        mock_ws.send = AsyncMock()

        with patch("dispatch.agents.anthem.asyncio.wait_for", side_effect=asyncio.TimeoutError):
            with pytest.raises(AgentError):
                await agent.send("hello")

        assert len(agent._pending) == 0

    async def test_send_connection_closed_raises(self, agent):
        mock_ws = AsyncMock()
        agent._ws = mock_ws

        import websockets

        mock_ws.send = AsyncMock(side_effect=websockets.ConnectionClosed(None, None))

        with pytest.raises(AgentError, match="closed"):
            await agent.send("hello")


# -- Disconnect ----------------------------------------------------------------


class TestDisconnect:
    async def test_disconnect_closes_ws(self, agent):
        mock_ws = AsyncMock()
        mock_ws.close = AsyncMock()
        agent._ws = mock_ws

        await agent.disconnect()

        mock_ws.close.assert_called_once()
        assert agent._ws is None

    async def test_disconnect_without_ws(self, agent):
        await agent.disconnect()
        assert agent._ws is None

    async def test_disconnect_cancels_recv_task(self, agent):
        mock_ws = AsyncMock()
        mock_ws.close = AsyncMock()
        agent._ws = mock_ws

        async def fake_recv_loop():
            await asyncio.sleep(3600)

        agent._recv_task = asyncio.create_task(fake_recv_loop())
        await agent.disconnect()

        assert agent._recv_task is None

    async def test_disconnect_fails_pending_futures(self, agent):
        fut = asyncio.get_running_loop().create_future()
        agent._pending["test-id"] = fut

        await agent.disconnect()

        with pytest.raises(AgentError, match="disconnected"):
            await fut
        assert len(agent._pending) == 0


# -- Response handling ---------------------------------------------------------


class TestHandleResponse:
    async def test_response_resolves_pending_future(self, agent):
        req_id = "req-123"
        fut = asyncio.get_running_loop().create_future()
        agent._pending[req_id] = fut

        agent._handle_response(
            {
                "type": "res",
                "id": req_id,
                "text": "Task dispatched successfully.",
            }
        )

        result = await fut
        assert result == "Task dispatched successfully."

    async def test_response_error_sets_exception(self, agent):
        req_id = "req-456"
        fut = asyncio.get_running_loop().create_future()
        agent._pending[req_id] = fut

        agent._handle_response(
            {
                "type": "res",
                "id": req_id,
                "error": "orchestrator unavailable",
            }
        )

        with pytest.raises(AgentError, match="orchestrator unavailable"):
            await fut

    async def test_response_unknown_id_ignored(self, agent):
        agent._handle_response(
            {
                "type": "res",
                "id": "unknown-id",
                "text": "orphan response",
            }
        )

    async def test_response_missing_text_uses_default(self, agent):
        req_id = "req-789"
        fut = asyncio.get_running_loop().create_future()
        agent._pending[req_id] = fut

        agent._handle_response(
            {
                "type": "res",
                "id": req_id,
            }
        )

        result = await fut
        assert result == "No response received."

    async def test_ack_response_does_not_resolve_future(self, agent):
        req_id = "req-ack-1"
        fut = asyncio.get_running_loop().create_future()
        agent._pending[req_id] = fut

        agent._handle_response(
            {
                "type": "res",
                "id": req_id,
                "ack": True,
            }
        )

        assert not fut.done(), "ack should not resolve the pending future"
        assert req_id in agent._pending, "pending entry should be preserved for followup"

    async def test_ack_then_followup_resolves_future(self, agent):
        req_id = "req-ack-2"
        fut = asyncio.get_running_loop().create_future()
        agent._pending[req_id] = fut

        agent._handle_response(
            {
                "type": "res",
                "id": req_id,
                "ack": True,
            }
        )

        assert not fut.done()

        await agent._handle_event(
            {
                "type": "event",
                "event": "channel.followup",
                "thread": req_id,
                "text": "Sure, scaffolding prism now.",
            }
        )

        result = await fut
        assert result == "Sure, scaffolding prism now."


# -- Event handling ------------------------------------------------------------


class TestHandleEvent:
    async def test_task_completed_queues_notification(self, agent):
        queue = NotificationQueue()
        agent._notification_queue = queue

        await agent._handle_event(
            {
                "type": "event",
                "event": "task.completed",
                "text": "Task GH-42 completed: Add CONTRIBUTING.md ($0.058)",
            }
        )

        notif = queue.get_nowait()
        assert notif.text == "Task GH-42 completed: Add CONTRIBUTING.md ($0.058)"
        assert notif.agent_name == "anthem"
        assert notif.priority == 1

    async def test_task_failed_is_urgent(self, agent):
        queue = NotificationQueue()
        agent._notification_queue = queue

        await agent._handle_event(
            {
                "type": "event",
                "event": "task.failed",
                "text": "Task GH-99 failed after 3 retries",
            }
        )

        notif = queue.get_nowait()
        assert notif.priority == 0

    async def test_maintenance_suggested_is_urgent(self, agent):
        queue = NotificationQueue()
        agent._notification_queue = queue

        await agent._handle_event(
            {
                "type": "event",
                "event": "maintenance.suggested",
                "text": "3 failures in 24h on label 'bug'",
            }
        )

        notif = queue.get_nowait()
        assert notif.priority == 0

    async def test_wave_completed_is_normal_priority(self, agent):
        queue = NotificationQueue()
        agent._notification_queue = queue

        await agent._handle_event(
            {
                "type": "event",
                "event": "wave.completed",
                "text": "Wave 2 complete, 4 tasks done",
            }
        )

        notif = queue.get_nowait()
        assert notif.priority == 1

    async def test_empty_text_event_ignored(self, agent):
        queue = NotificationQueue()
        agent._notification_queue = queue

        await agent._handle_event(
            {
                "type": "event",
                "event": "task.completed",
                "text": "",
            }
        )

        assert queue.empty()

    async def test_event_without_queue_logs_warning(self, agent):
        await agent._handle_event(
            {
                "type": "event",
                "event": "task.completed",
                "text": "Task finished",
            }
        )

    async def test_followup_with_matching_thread_resolves_future(self, agent):
        queue = NotificationQueue()
        agent._notification_queue = queue

        req_id = "req-followup-1"
        fut = asyncio.get_running_loop().create_future()
        agent._pending[req_id] = fut

        await agent._handle_event(
            {
                "type": "event",
                "event": "channel.followup",
                "thread": req_id,
                "text": "Here is the real response.",
            }
        )

        result = await fut
        assert result == "Here is the real response."
        assert queue.empty(), "followup should not be queued as notification"

    async def test_followup_without_thread_queues_notification(self, agent):
        queue = NotificationQueue()
        agent._notification_queue = queue

        await agent._handle_event(
            {
                "type": "event",
                "event": "channel.followup",
                "text": "Unsolicited followup.",
            }
        )

        notif = queue.get_nowait()
        assert notif.text == "Unsolicited followup."

    async def test_followup_with_unknown_thread_queues_notification(self, agent):
        queue = NotificationQueue()
        agent._notification_queue = queue

        await agent._handle_event(
            {
                "type": "event",
                "event": "channel.followup",
                "thread": "no-such-request",
                "text": "Stale followup.",
            }
        )

        notif = queue.get_nowait()
        assert notif.text == "Stale followup."


# -- Subscribe -----------------------------------------------------------------


class TestSubscribe:
    async def test_subscribe_stores_queue(self, agent):
        queue = NotificationQueue()
        await agent.subscribe(queue)
        assert agent._notification_queue is queue
