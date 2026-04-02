"""Anthem agent -- WebSocket protocol for orchestrator chat + event notifications.

Connects to Anthem's dispatch channel adapter via a single WebSocket.
Auth is a bearer token exchange on connect. Chat uses correlated req/res
frames. Anthem events (task.completed, etc.) arrive as push frames and
are queued as voice notifications.

Protocol reference (JSON text frames):
  Client -> Server: {"type":"auth","token":"<bearer>","client":"dispatch"}
  Server -> Client: {"type":"auth_ok"} | {"type":"auth_fail","error":"..."}
  Client -> Server: {"type":"req","id":"<uuid>","text":"..."}
  Server -> Client: {"type":"res","id":"<uuid>","text":"...","ack":bool}
  Server -> Client: {"type":"event","event":"<name>","text":"...","thread":"<id>"}

When ack=true on a res frame, the server is signaling that processing has
started but the real response will follow as a channel.followup event.
The followup carries a "thread" field to correlate back to the pending request.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from typing import TYPE_CHECKING

import websockets

from dispatch.agents.base import AgentError, BaseAgent, AgentRouter
from dispatch.notifications import Notification

if TYPE_CHECKING:
    from dispatch.notifications import NotificationQueue

logger = logging.getLogger(__name__)

_AUTH_TIMEOUT = 10.0
_SEND_TIMEOUT = 120.0
_MAX_BACKOFF = 30


def _is_expected_connect_failure(exc: Exception) -> bool:
    """Return True for startup-order failures that should log cleanly."""
    return isinstance(
        exc,
        (
            AgentError,
            OSError,
            asyncio.TimeoutError,
            websockets.InvalidHandshake,
            websockets.InvalidURI,
        ),
    )


class AnthemAgent(BaseAgent):
    def __init__(self, name: str, voice: str, endpoint: str, token_env: str) -> None:
        super().__init__(name, voice)
        self._endpoint = endpoint.rstrip("/")
        self._token_env = token_env
        self._ws = None
        self._pending: dict[str, asyncio.Future] = {}
        self._recv_task: asyncio.Task | None = None
        self._notification_queue: NotificationQueue | None = None

    @property
    def _token(self) -> str:
        return os.environ.get(self._token_env, "")

    @property
    def _ws_uri(self) -> str:
        ep = self._endpoint
        if ep.startswith("http://"):
            return ep.replace("http://", "ws://", 1)
        if ep.startswith("https://"):
            return ep.replace("https://", "wss://", 1)
        return ep

    # -- BaseAgent interface ---------------------------------------------------

    async def connect(self) -> None:
        try:
            self._ws = await websockets.connect(self._ws_uri)
            await self._authenticate()
            logger.info("Anthem connected to %s", self._ws_uri)
        except Exception as exc:
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:
                    pass
            self._ws = None
            if _is_expected_connect_failure(exc):
                logger.warning(
                    "Anthem unavailable at %s: %s -- agent degraded, retrying in background",
                    self._ws_uri,
                    exc,
                )
            else:
                logger.warning(
                    "Anthem connection failed at %s -- agent degraded, retrying in background",
                    self._ws_uri,
                    exc_info=True,
                )
        if self._recv_task is None or self._recv_task.done():
            self._recv_task = asyncio.create_task(self._recv_loop())

    async def send(self, text: str) -> str:
        if self._ws is None:
            raise AgentError("Anthem WebSocket not connected")

        req_id = uuid.uuid4().hex
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future

        try:
            await self._ws.send(json.dumps({
                "type": "req",
                "id": req_id,
                "text": text,
            }))
            return await asyncio.wait_for(future, timeout=_SEND_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise AgentError("Anthem request timed out") from exc
        except websockets.ConnectionClosed as exc:
            raise AgentError(f"Anthem WebSocket closed: {exc}") from exc
        except Exception as exc:
            raise AgentError(f"Anthem request failed: {exc}") from exc
        finally:
            self._pending.pop(req_id, None)

    async def disconnect(self) -> None:
        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
            self._recv_task = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        self._fail_pending()

    async def subscribe(self, queue: NotificationQueue) -> None:
        self._notification_queue = queue

    # -- Auth ------------------------------------------------------------------

    async def _authenticate(self) -> None:
        await self._ws.send(json.dumps({
            "type": "auth",
            "token": self._token,
            "client": "dispatch",
        }))
        raw = await asyncio.wait_for(self._ws.recv(), timeout=_AUTH_TIMEOUT)
        msg = json.loads(raw)
        if msg.get("type") == "auth_ok":
            return
        error = msg.get("error", "unknown error")
        raise AgentError(f"Anthem auth failed: {error}")

    # -- Pending management ----------------------------------------------------

    def _fail_pending(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(AgentError("Anthem WebSocket disconnected"))
        self._pending.clear()

    # -- Recv loop -------------------------------------------------------------

    async def _recv_loop(self) -> None:
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
                                logger.warning("Malformed frame from Anthem")
                                continue
                            logger.debug("Anthem frame: %s", msg)
                            msg_type = msg.get("type")
                            if msg_type == "res":
                                self._handle_response(msg)
                            elif msg_type == "event":
                                await self._handle_event(msg)
                    except websockets.ConnectionClosed:
                        logger.warning("Anthem WebSocket closed")

                self._ws = None
                self._fail_pending()

                logger.debug("Reconnecting Anthem in %ds...", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)

                try:
                    self._ws = await websockets.connect(self._ws_uri)
                    await self._authenticate()
                    logger.info("Anthem reconnected to %s", self._ws_uri)
                except Exception as exc:
                    if _is_expected_connect_failure(exc):
                        logger.debug("Anthem reconnect failed: %s", exc)
                    else:
                        logger.debug("Anthem reconnect failed", exc_info=True)
                    if self._ws is not None:
                        try:
                            await self._ws.close()
                        except Exception:
                            pass
                        self._ws = None
        except asyncio.CancelledError:
            logger.info("Anthem recv loop cancelled")

    # -- Frame handlers --------------------------------------------------------

    def _handle_response(self, msg: dict) -> None:
        req_id = msg.get("id")
        if not req_id or req_id not in self._pending:
            return
        if msg.get("ack"):
            logger.debug("Anthem ack received for %s", req_id)
            return
        fut = self._pending[req_id]
        if msg.get("error"):
            if not fut.done():
                fut.set_exception(AgentError(f"Anthem error: {msg['error']}"))
        else:
            if not fut.done():
                fut.set_result(msg.get("text", "No response received."))

    async def _handle_event(self, msg: dict) -> None:
        text = msg.get("text", "")
        if not text:
            return
        event_type = msg.get("event", "event")

        thread_id = msg.get("thread", "")
        if event_type == "channel.followup" and thread_id and thread_id in self._pending:
            fut = self._pending[thread_id]
            if not fut.done():
                fut.set_result(text)
            return

        priority = 0 if event_type in ("task.failed", "maintenance.suggested") else 1
        await self._enqueue_notification(text, priority)

    # -- Notification helpers --------------------------------------------------

    async def _enqueue_notification(self, text: str, priority: int = 1) -> None:
        if self._notification_queue is None:
            logger.warning("Anthem notification dropped (no queue): %s", text[:80])
            return
        notif = Notification(
            priority=priority,
            timestamp=time.time(),
            agent_name=self.name,
            agent_voice=self.voice,
            text=text,
        )
        await self._notification_queue.put(notif)
        logger.info("Anthem notification: %s", text[:80])


AgentRouter.register("anthem", AnthemAgent)
