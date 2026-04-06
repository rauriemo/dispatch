"""OpenClaw agent -- WebSocket gateway protocol for chat + push notifications.

Dual-connection architecture:
  1. Operator connection (cli mode) -- chat.send, receive responses
  2. Node connection (voice capability) -- receive proactive invoke commands
"""

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

from dispatch.agents.base import AgentError, AgentRouter, BaseAgent
from dispatch.crypto import (
    device_fingerprint,
    load_or_create_key,
    public_key_b64,
    sign_payload,
)
from dispatch.notifications import Notification

if TYPE_CHECKING:
    from dispatch.notifications import NotificationQueue

logger = logging.getLogger(__name__)

_OPERATOR_CLIENT_ID = "cli"
_NODE_CLIENT_ID = "node-host"
_CLIENT_VERSION = "0.1.0"
_PROTOCOL_VERSION = 3


class OpenClawAgent(BaseAgent):
    def __init__(self, name: str, voice: str, endpoint: str, token_env: str) -> None:
        super().__init__(name, voice)
        self.endpoint = endpoint.rstrip("/")
        self.token_env = token_env
        self._http = httpx.AsyncClient(timeout=10.0)
        self._ws = None
        self._node_ws = None
        self._device_key = load_or_create_key()
        self._pending: dict[str, asyncio.Future] = {}
        self._completed_runs: set[str] = set()
        self._recv_task: asyncio.Task | None = None
        self._node_recv_task: asyncio.Task | None = None
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

        # Operator WebSocket (chat)
        try:
            self._ws = await websockets.connect(self._ws_uri)
            await self._handshake()
            self._recv_task = asyncio.create_task(self._recv_loop())
            logger.info("OpenClaw operator connected to %s", self._ws_uri)
        except Exception:
            logger.warning(
                "OpenClaw operator handshake failed at %s -- agent degraded",
                self._ws_uri,
                exc_info=True,
            )
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:
                    pass
            self._ws = None

        # Node WebSocket (voice capability for proactive push)
        await self._connect_node()

    async def _connect_node(self) -> None:
        """Open a second WebSocket as a node with voice capability."""
        try:
            self._node_ws = await websockets.connect(self._ws_uri)
            await self._node_handshake()
            self._node_recv_task = asyncio.create_task(self._node_recv_loop())
            logger.info("OpenClaw node connected to %s (caps: voice)", self._ws_uri)
        except Exception:
            logger.warning(
                "OpenClaw node handshake failed at %s -- voice push unavailable",
                self._ws_uri,
                exc_info=True,
            )
            if self._node_ws is not None:
                try:
                    await self._node_ws.close()
                except Exception:
                    pass
            self._node_ws = None

    async def send(self, text: str) -> str:
        if self._ws is None:
            raise AgentError("OpenClaw WebSocket not connected")

        req_id = uuid.uuid4().hex
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future

        try:
            await self._ws.send(
                json.dumps(
                    {
                        "type": "req",
                        "id": req_id,
                        "method": "chat.send",
                        "params": {
                            "sessionKey": self._session_key,
                            "message": text,
                            "idempotencyKey": req_id,
                        },
                    }
                )
            )
            return await asyncio.wait_for(future, timeout=60.0)
        except asyncio.TimeoutError as exc:
            raise AgentError("OpenClaw request timed out") from exc
        except websockets.ConnectionClosed as exc:
            raise AgentError(f"OpenClaw WebSocket closed: {exc}") from exc
        except Exception as exc:
            raise AgentError(f"OpenClaw request failed: {exc}") from exc
        finally:
            self._pending.pop(req_id, None)

    async def disconnect(self) -> None:
        for task in (self._recv_task, self._node_recv_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        for ws in (self._ws, self._node_ws):
            if ws is not None:
                await ws.close()
        self._ws = None
        self._node_ws = None
        self._fail_pending()
        await self._http.aclose()

    async def subscribe(self, queue: NotificationQueue) -> None:
        self._notification_queue = queue

    # -- Handshake -------------------------------------------------------------

    async def _perform_handshake(
        self,
        ws,
        *,
        role: str,
        client_id: str,
        client_mode: str,
        scopes: list[str],
        caps: list[str] | None = None,
        commands: list[str] | None = None,
        permissions: dict | None = None,
    ) -> None:
        """Complete the gateway connect handshake on a given WebSocket."""
        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
        challenge = json.loads(raw)
        if challenge.get("event") != "connect.challenge":
            raise AgentError(f"Expected connect.challenge, got: {challenge.get('event')}")

        nonce = challenge["payload"]["nonce"]

        fp = device_fingerprint(self._device_key)
        pub_b64 = public_key_b64(self._device_key)
        signed_at_ms = int(time.time() * 1000)
        token = self._token

        sig_payload = "|".join(
            [
                "v2",
                fp,
                client_id,
                client_mode,
                role,
                ",".join(scopes),
                str(signed_at_ms),
                token or "",
                nonce,
            ]
        )
        signature = sign_payload(self._device_key, sig_payload)

        connect_req = {
            "type": "req",
            "id": uuid.uuid4().hex,
            "method": "connect",
            "params": {
                "minProtocol": _PROTOCOL_VERSION,
                "maxProtocol": _PROTOCOL_VERSION,
                "client": {
                    "id": client_id,
                    "version": _CLIENT_VERSION,
                    "platform": "windows",
                    "mode": client_mode,
                },
                "role": role,
                "scopes": scopes,
                "caps": caps or [],
                "commands": commands or [],
                "permissions": permissions or {},
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
        await ws.send(json.dumps(connect_req))

        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
        res = json.loads(raw)
        if not res.get("ok"):
            raise AgentError(f"OpenClaw connect rejected: {res}")
        logger.info("OpenClaw %s handshake complete (protocol %d)", role, _PROTOCOL_VERSION)

    async def _handshake(self) -> None:
        """Operator handshake on self._ws."""
        await self._perform_handshake(
            self._ws,
            role="operator",
            client_id=_OPERATOR_CLIENT_ID,
            client_mode=_OPERATOR_CLIENT_ID,
            scopes=["operator.read", "operator.write"],
        )

    async def _node_handshake(self) -> None:
        """Node handshake on self._node_ws."""
        await self._perform_handshake(
            self._node_ws,
            role="node",
            client_id=_NODE_CLIENT_ID,
            client_mode="node",
            scopes=[],
            caps=["voice"],
            commands=["voice.speak"],
            permissions={"voice.speak": True},
        )

    # -- Pending management ----------------------------------------------------

    def _fail_pending(self) -> None:
        """Resolve all pending request futures with a disconnect error."""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(AgentError("OpenClaw WebSocket disconnected"))
        self._pending.clear()

    # -- Operator recv loop ----------------------------------------------------

    async def _recv_loop(self) -> None:
        """Background task: dispatch incoming operator frames, auto-reconnect."""
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
                        "Reconnect failed for %s",
                        self.name,
                        exc_info=True,
                    )
                    if self._ws is not None:
                        try:
                            await self._ws.close()
                        except Exception:
                            pass
                        self._ws = None
        except asyncio.CancelledError:
            logger.info("WebSocket recv loop for %s cancelled", self.name)

    # -- Node recv loop --------------------------------------------------------

    async def _node_recv_loop(self) -> None:
        """Background task: handle invoke commands on the node connection."""
        backoff = 1
        try:
            while True:
                if self._node_ws is not None:
                    try:
                        async for raw in self._node_ws:
                            backoff = 1
                            try:
                                msg = json.loads(raw)
                            except json.JSONDecodeError:
                                logger.warning("Malformed node frame from %s", self.name)
                                continue

                            logger.debug("Node frame: %s", msg)
                            if msg.get("type") == "req":
                                await self._handle_invoke(msg)
                    except websockets.ConnectionClosed:
                        logger.warning("Node WebSocket closed for %s", self.name)

                self._node_ws = None

                logger.info("Reconnecting node %s in %ds...", self.name, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

                try:
                    self._node_ws = await websockets.connect(self._ws_uri)
                    await self._node_handshake()
                    logger.info("OpenClaw node reconnected to %s", self._ws_uri)
                except Exception:
                    logger.warning(
                        "Node reconnect failed for %s",
                        self.name,
                        exc_info=True,
                    )
                    if self._node_ws is not None:
                        try:
                            await self._node_ws.close()
                        except Exception:
                            pass
                        self._node_ws = None
        except asyncio.CancelledError:
            logger.info("Node recv loop for %s cancelled", self.name)

    # -- Event/invoke handlers -------------------------------------------------

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

        elif event_name == "agent" and payload.get("stream") == "lifecycle":
            phase = payload.get("data", {}).get("phase")
            if phase == "end" and run_id and run_id in self._pending:
                self._completed_runs.add(run_id)
                fut = self._pending[run_id]
                if not fut.done():
                    text = "".join(getattr(fut, "_text_buf", []))
                    fut.set_result(text or "No response text received.")

        elif event_name == "chat" and payload.get("state") == "final":
            message = payload.get("message", {})
            content = message.get("content", [])
            text = "".join(c.get("text", "") for c in content if c.get("type") == "text")

            if run_id and run_id in self._pending:
                fut = self._pending[run_id]
                if not text:
                    text = "".join(getattr(fut, "_text_buf", []))
                if not fut.done():
                    fut.set_result(text or "No response text received.")
                self._completed_runs.add(run_id)
            elif run_id and run_id in self._completed_runs:
                self._completed_runs.discard(run_id)
            elif text:
                await self._enqueue_notification(text)

        elif event_name == "notification":
            await self._handle_notification(payload)

    async def _handle_invoke(self, msg: dict) -> None:
        """Handle an invoke command from the gateway on the node connection."""
        req_id = msg.get("id")
        params = msg.get("params", {})
        command = params.get("command", "")

        if command == "voice.speak":
            text = params.get("args", {}).get("text", "")
            if text:
                await self._enqueue_notification(text, priority=0)
            if req_id and self._node_ws:
                await self._node_ws.send(
                    json.dumps(
                        {
                            "type": "res",
                            "id": req_id,
                            "ok": True,
                            "payload": {},
                        }
                    )
                )
        else:
            logger.warning("Unknown invoke command from gateway: %s", command)
            if req_id and self._node_ws:
                await self._node_ws.send(
                    json.dumps(
                        {
                            "type": "res",
                            "id": req_id,
                            "ok": False,
                            "error": {
                                "code": "UNKNOWN_COMMAND",
                                "message": f"unknown: {command}",
                            },
                        }
                    )
                )

    def _handle_response(self, msg: dict) -> None:
        req_id = msg.get("id")
        if not req_id or req_id not in self._pending:
            return

        if not msg.get("ok"):
            fut = self._pending[req_id]
            if not fut.done():
                fut.set_exception(AgentError(f"OpenClaw error: {msg.get('error') or msg.get('payload')}"))

    # -- Notification helpers --------------------------------------------------

    async def _enqueue_notification(self, text: str, priority: int = 1) -> None:
        """Push a notification to the queue for TTS playback by the main loop."""
        if self._notification_queue is None:
            logger.warning("Notification dropped (no queue): %s", text[:80])
            return
        notif = Notification(
            priority=priority,
            timestamp=time.time(),
            agent_name=self.name,
            agent_voice=self.voice,
            text=text,
        )
        await self._notification_queue.put(notif)
        logger.info("Notification from %s: %s", self.name, text[:80])

    async def _handle_notification(self, payload: dict) -> None:
        priority = 0 if payload.get("urgent") else 1
        await self._enqueue_notification(payload.get("text", ""), priority)


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


AgentRouter.register("openclaw", OpenClawAgent)
