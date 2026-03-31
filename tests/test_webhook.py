"""Tests for dispatch.webhook -- POST /notify endpoint."""

import json

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer
from unittest.mock import patch

from dispatch.notifications import NotificationQueue
from dispatch.webhook import _create_app, WebhookServer


AGENT_VOICES = {"navi": "en-US-AvaMultilingualNeural", "jarvis": "en-US-EricNeural"}


@pytest.fixture
def notification_queue():
    return NotificationQueue()


@pytest.fixture
def app(notification_queue):
    """Create webhook app with no auth secret."""
    with patch.dict("os.environ", {}, clear=False):
        # Ensure DISPATCH_WEBHOOK_SECRET is unset
        import os
        os.environ.pop("DISPATCH_WEBHOOK_SECRET", None)
        return _create_app(notification_queue, AGENT_VOICES)


@pytest.fixture
def app_with_auth(notification_queue):
    """Create webhook app with auth secret set."""
    with patch.dict("os.environ", {"DISPATCH_WEBHOOK_SECRET": "my-secret"}):
        return _create_app(notification_queue, AGENT_VOICES)


@pytest.fixture
async def client(app):
    async with TestClient(TestServer(app)) as c:
        yield c


@pytest.fixture
async def auth_client(app_with_auth):
    async with TestClient(TestServer(app_with_auth)) as c:
        yield c


class TestValidPayload:
    async def test_valid_payload_queues_notification(self, client, notification_queue):
        """Valid POST /notify should return 200 and queue a notification."""
        resp = await client.post("/notify", json={
            "agent": "navi", "text": "Time for standup!", "priority": 1,
        })
        assert resp.status == 200
        body = await resp.json()
        assert body == {"ok": True}

        notif = notification_queue.get_nowait()
        assert notif.agent_name == "navi"
        assert notif.agent_voice == "en-US-AvaMultilingualNeural"
        assert notif.text == "Time for standup!"
        assert notif.priority == 1

    async def test_default_priority_is_one(self, client, notification_queue):
        """Omitting priority should default to 1."""
        resp = await client.post("/notify", json={
            "agent": "navi", "text": "Hello",
        })
        assert resp.status == 200

        notif = notification_queue.get_nowait()
        assert notif.priority == 1

    async def test_urgent_priority(self, client, notification_queue):
        """Priority 0 should be preserved."""
        resp = await client.post("/notify", json={
            "agent": "jarvis", "text": "Alert!", "priority": 0,
        })
        assert resp.status == 200

        notif = notification_queue.get_nowait()
        assert notif.priority == 0
        assert notif.agent_name == "jarvis"
        assert notif.agent_voice == "en-US-EricNeural"


class TestValidation:
    async def test_missing_agent_returns_400(self, client):
        resp = await client.post("/notify", json={"text": "hello"})
        assert resp.status == 400
        body = await resp.json()
        assert body["ok"] is False
        assert "agent" in body["error"]

    async def test_missing_text_returns_400(self, client):
        resp = await client.post("/notify", json={"agent": "navi"})
        assert resp.status == 400
        body = await resp.json()
        assert body["ok"] is False
        assert "text" in body["error"]

    async def test_empty_text_returns_400(self, client):
        resp = await client.post("/notify", json={"agent": "navi", "text": ""})
        assert resp.status == 400
        body = await resp.json()
        assert body["ok"] is False

    async def test_whitespace_only_text_returns_400(self, client):
        resp = await client.post("/notify", json={"agent": "navi", "text": "   "})
        assert resp.status == 400

    async def test_unknown_agent_returns_404(self, client):
        resp = await client.post("/notify", json={"agent": "unknown", "text": "hi"})
        assert resp.status == 404
        body = await resp.json()
        assert body["ok"] is False
        assert "unknown agent" in body["error"]

    async def test_malformed_json_returns_400(self, client):
        resp = await client.post(
            "/notify",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
        body = await resp.json()
        assert body["ok"] is False

    async def test_json_array_returns_400(self, client):
        resp = await client.post("/notify", json=[1, 2, 3])
        assert resp.status == 400


class TestAuth:
    async def test_auth_required_when_secret_set(self, auth_client):
        """Missing auth header should return 401 when secret is configured."""
        resp = await auth_client.post("/notify", json={
            "agent": "navi", "text": "hello",
        })
        assert resp.status == 401
        body = await resp.json()
        assert body["error"] == "unauthorized"

    async def test_wrong_token_returns_401(self, auth_client):
        resp = await auth_client.post(
            "/notify",
            json={"agent": "navi", "text": "hello"},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status == 401

    async def test_correct_token_succeeds(self, auth_client, notification_queue):
        resp = await auth_client.post(
            "/notify",
            json={"agent": "navi", "text": "hello"},
            headers={"Authorization": "Bearer my-secret"},
        )
        assert resp.status == 200
        assert not notification_queue.empty()

    async def test_no_auth_needed_when_secret_unset(self, client, notification_queue):
        """When DISPATCH_WEBHOOK_SECRET is not set, requests pass without auth."""
        resp = await client.post("/notify", json={
            "agent": "navi", "text": "hello",
        })
        assert resp.status == 200


class TestRouting:
    async def test_get_notify_returns_405(self, client):
        resp = await client.get("/notify")
        assert resp.status == 405

    async def test_wrong_path_returns_404(self, client):
        resp = await client.post("/wrong", json={"agent": "navi", "text": "hi"})
        assert resp.status == 404


class TestEndToEnd:
    async def test_full_cycle(self, client, notification_queue):
        """Full request/response cycle: POST, check response, verify notification."""
        resp = await client.post("/notify", json={
            "agent": "navi", "text": "Deploy complete", "priority": 0,
        })
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True

        notif = notification_queue.get_nowait()
        assert notif.agent_name == "navi"
        assert notif.agent_voice == "en-US-AvaMultilingualNeural"
        assert notif.text == "Deploy complete"
        assert notif.priority == 0
        assert notif.timestamp > 0
