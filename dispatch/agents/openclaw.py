"""OpenClaw agent -- WebSocket gateway protocol for chat + push notifications."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import TYPE_CHECKING

import httpx
import websockets

from dispatch.agents.base import AgentError, BaseAgent, AgentRouter
from dispatch.crypto import device_fingerprint, load_or_create_key, public_key_b64, sign_payload
from dispatch.notifications import Notification

if TYPE_CHECKING:
    from dispatch.notifications import NotificationQueue

logger = logging.getLogger(__name__)

_CLIENT_TYPE = "cli"
_CLIENT_VERSION = "0.1.0"
_PROTOCOL_VERSION = 3


class OpenClawAgent(BaseAgent):
    def __init__(self, name: str, voice: str, endpoint: str, token_env: str) -> None:
        super().__init__(name, voice)
        self.endpoint = endpoint.rstrip("/")
        self.token_env = token_env
        self._http = httpx.AsyncClient(timeout=10.0)
        self._ws = None
        self._device_key = load_or_create_key()
        self._pending: dict[str, asyncio.Future] = {}
        self._recv_task: asyncio.Task | None = None
        self._notification_queue: NotificationQueue | None = None
        self._session_key: str = uuid.uuid4().hex

    @property
    def _token(self) -> str:
        return os.environ.get(self.token_env, "")

    @property
    def _ws_uri(self) -> str:
        return self.endpoint.replace("http://", "ws://").replace("https://", "wss://")

    # -- BaseAgent interface ---------------------------------------------------

    async def connect(self) -> None:
        # Health check (best-effort)
        try:
            resp = await self._http.get(f"{self.endpoint}/healthz")
            resp.raise_for_status()
            logger.info("OpenClaw endpoint reachable at %s", self.endpoint)
        except Exception:
            logger.warning(
                "OpenClaw endpoint %s not reachable -- agent degraded",
                self.endpoint,
                exc_info=True,
            )
            return

        # WebSocket handshake
        try:
            self._ws = await websockets.connect(self._ws_uri)
            await self._handshake()
            self._recv_task = asyncio.create_task(self._recv_loop())
            logger.info("OpenClaw WebSocket connected to %s", self._ws_uri)
        except Exception:
            logger.warning(
                "OpenClaw WebSocket handshake failed at %s -- agent degraded",
                self._ws_uri,
                exc_info=True,
            )
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:
                    pass
            self._ws = None

    async def send(self, text: str) -> str:
        if self._ws is None:
            raise AgentError("OpenClaw WebSocket not connected")

        req_id = uuid.uuid4().hex
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future

        try:
            await self._ws.send(json.dumps({
                "type": "req",
                "id": req_id,
                "method": "chat.send",
                "params": {
                    "sessionKey": self._session_key,
                    "message": text,
                    "idempotencyKey": req_id,
                },
            }))
            return await asyncio.wait_for(future, timeout=30.0)
        except asyncio.TimeoutError as exc:
            raise AgentError("OpenClaw request timed out") from exc
        except websockets.ConnectionClosed as exc:
            raise AgentError(f"OpenClaw WebSocket closed: {exc}") from exc
        except Exception as exc:
            raise AgentError(f"OpenClaw request failed: {exc}") from exc
        finally:
            self._pending.pop(req_id, None)

    async def disconnect(self) -> None:
        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        self._fail_pending()
        await self._http.aclose()

    async def subscribe(self, queue: NotificationQueue) -> None:
        self._notification_queue = queue

    # -- Internal --------------------------------------------------------------

    async def _handshake(self) -> None:
        """Complete the gateway connect handshake."""
        # 1. Receive challenge
        raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
        challenge = json.loads(raw)
        if challenge.get("event") != "connect.challenge":
            raise AgentError(f"Expected connect.challenge, got: {challenge.get('event')}")

        nonce = challenge["payload"]["nonce"]

        # 2. Build v2 signature payload and sign
        fp = device_fingerprint(self._device_key)
        pub_b64 = public_key_b64(self._device_key)
        signed_at_ms = int(time.time() * 1000)
        role = "operator"
        scopes = ["operator.read", "operator.write"]
        token = self._token

        sig_payload = "|".join([
            "v2", fp, _CLIENT_TYPE, _CLIENT_TYPE, role,
            ",".join(scopes), str(signed_at_ms), token or "", nonce,
        ])
        signature = sign_payload(self._device_key, sig_payload)

        req_id = uuid.uuid4().hex
        connect_req = {
            "type": "req",
            "id": req_id,
            "method": "connect",
            "params": {
                "minProtocol": _PROTOCOL_VERSION,
                "maxProtocol": _PROTOCOL_VERSION,
                "client": {
                    "id": _CLIENT_TYPE,
                    "version": _CLIENT_VERSION,
                    "platform": "windows",
                    "mode": _CLIENT_TYPE,
                },
                "role": role,
                "scopes": scopes,
                "caps": [],
                "commands": [],
                "permissions": {},
                "auth": {"token": token},
                "locale": "en-US",
                "userAgent": f"dispatch/{_CLIENT_VERSION}",
                "device": {
                    "id": fp,
                    "publicKey": pub_b64,
                    "signature": signature,
                    "signedAt": signed_at_ms,
                    "nonce": nonce,
                },
            },
        }
        await self._ws.send(json.dumps(connect_req))

        # 3. Receive hello-ok
        raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
        res = json.loads(raw)
        if not res.get("ok"):
            raise AgentError(f"OpenClaw connect rejected: {res}")
        logger.info("OpenClaw handshake complete (protocol %d)", _PROTOCOL_VERSION)

    def _fail_pending(self) -> None:
        """Resolve all pending request futures with a disconnect error."""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(AgentError("OpenClaw WebSocket disconnected"))
        self._pending.clear()

    async def _recv_loop(self) -> None:
        """Background task: dispatch incoming frames, auto-reconnect on disconnect."""
        backoff = 1
        try:
            while True:
                if self._ws is not None:
                    try:
                        async for raw in self._ws:
                            backoff = 1
                            try:
                                msg = json.loads(raw)
                            except json.JSONDecodeError:
                                logger.warning("Malformed WebSocket frame from %s", self.name)
                                continue

                            msg_type = msg.get("type")
                            logger.debug("WS frame: %s", msg)
                            if msg_type == "event":
                                await self._handle_event(msg)
                            elif msg_type == "res":
                                self._handle_response(msg)
                    except websockets.ConnectionClosed:
                        logger.warning("WebSocket closed for %s", self.name)

                self._ws = None
                self._fail_pending()

                logger.info("Reconnecting %s in %ds...", self.name, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

                try:
                    self._ws = await websockets.connect(self._ws_uri)
                    await self._handshake()
                    logger.info("OpenClaw WebSocket reconnected to %s", self._ws_uri)
                except Exception:
                    logger.warning(
                        "Reconnect failed for %s", self.name, exc_info=True,
                    )
                    if self._ws is not None:
                        try:
                            await self._ws.close()
                        except Exception:
                            pass
                        self._ws = None
        except asyncio.CancelledError:
            logger.info("WebSocket recv loop for %s cancelled", self.name)

    async def _handle_event(self, msg: dict) -> None:
        event_name = msg.get("event", "")
        payload = msg.get("payload", {})
        run_id = payload.get("runId")

        if event_name == "agent" and payload.get("stream") == "assistant":
            if run_id and run_id in self._pending:
                fut = self._pending[run_id]
                if not hasattr(fut, "_text_buf"):
                    fut._text_buf = []  # type: ignore[attr-defined]
                delta = payload.get("data", {}).get("delta", "")
                if delta:
                    fut._text_buf.append(delta)  # type: ignore[attr-defined]

        elif event_name == "chat" and payload.get("state") == "final":
            if run_id and run_id in self._pending:
                fut = self._pending[run_id]
                message = payload.get("message", {})
                content = message.get("content", [])
                text = "".join(
                    c.get("text", "") for c in content if c.get("type") == "text"
                )
                if not text:
                    text = "".join(getattr(fut, "_text_buf", []))
                if not fut.done():
                    fut.set_result(text or "No response text received.")

        elif event_name == "notification":
            await self._handle_notification(payload)

    def _handle_response(self, msg: dict) -> None:
        req_id = msg.get("id")
        if not req_id or req_id not in self._pending:
            return

        if not msg.get("ok"):
            fut = self._pending[req_id]
            if not fut.done():
                fut.set_exception(AgentError(f"OpenClaw error: {msg.get('error') or msg.get('payload')}"))

    async def _handle_notification(self, payload: dict) -> None:
        if self._notification_queue is None:
            return
        priority = 0 if payload.get("urgent") else 1
        notif = Notification(
            priority=priority,
            timestamp=time.time(),
            agent_name=self.name,
            agent_voice=self.voice,
            text=payload.get("text", ""),
        )
        await self._notification_queue.put(notif)
        logger.info("Notification from %s: %s", self.name, notif.text[:80])


# -- HTTP fallback (disabled) -------------------------------------------------
# The HTTP POST /v1/responses endpoint returns 403 "missing scope: operator.write"
# due to a token-mode scope restoration bug in OpenClaw 2026.3.30 (GitHub #46650).
# Once fixed, this can be re-enabled as an alternative to the WebSocket protocol.
#
# async def _send_http(self, text: str) -> str:
#     resp = await self._http.post(
#         f"{self.endpoint}/v1/responses",
#         json={"model": "openclaw", "input": text},
#         headers={"Authorization": f"Bearer {self._token}"},
#     )
#     resp.raise_for_status()
#     data = resp.json()
#     for output in data.get("output", []):
#         for content in output.get("content", []):
#             if "text" in content:
#                 return content["text"]
#     return "No response text received."


# Register with the router
AgentRouter.register("openclaw", OpenClawAgent)
