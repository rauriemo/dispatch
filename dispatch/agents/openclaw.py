"""OpenClaw agent -- POST /v1/responses + WebSocket push notifications."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import TYPE_CHECKING

import httpx
import websockets

from dispatch.agents.base import AgentError, BaseAgent, AgentRouter
from dispatch.notifications import Notification

if TYPE_CHECKING:
    from dispatch.notifications import NotificationQueue

logger = logging.getLogger(__name__)


class OpenClawAgent(BaseAgent):
    def __init__(self, name: str, voice: str, endpoint: str, token_env: str) -> None:
        super().__init__(name, voice)
        self.endpoint = endpoint.rstrip("/")
        self.token_env = token_env
        self._client = httpx.AsyncClient(timeout=30.0)
        self._ws_task: asyncio.Task | None = None

    @property
    def _token(self) -> str:
        return os.environ.get(self.token_env, "")

    async def connect(self) -> None:
        try:
            resp = await self._client.get(
                f"{self.endpoint}/healthz",
                headers={"Authorization": f"Bearer {self._token}"},
            )
            resp.raise_for_status()
            logger.info("OpenClaw endpoint reachable at %s", self.endpoint)
        except Exception:
            logger.warning(
                "OpenClaw endpoint %s not reachable -- agent degraded",
                self.endpoint,
                exc_info=True,
            )

    async def send(self, text: str) -> str:
        try:
            resp = await self._client.post(
                f"{self.endpoint}/v1/responses",
                json={"model": "openclaw", "input": text},
                headers={"Authorization": f"Bearer {self._token}"},
            )
            resp.raise_for_status()
            data = resp.json()
            # Extract text from output[].content[].text
            for output in data.get("output", []):
                for content in output.get("content", []):
                    if "text" in content:
                        return content["text"]
            return "No response text received."
        except (httpx.HTTPError, KeyError, IndexError) as exc:
            raise AgentError(f"OpenClaw request failed: {exc}") from exc

    async def disconnect(self) -> None:
        if self._ws_task is not None:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        await self._client.aclose()

    async def subscribe(self, queue: NotificationQueue) -> None:
        """Start a background WebSocket listener for push notifications."""
        ws_uri = self.endpoint.replace("http://", "ws://").replace("https://", "wss://")
        ws_uri = f"{ws_uri}?token={self._token}"
        self._ws_task = asyncio.create_task(self._ws_listener(ws_uri, queue))

    async def _ws_listener(self, uri: str, queue: NotificationQueue) -> None:
        logger.info("Starting WebSocket listener for %s", self.name)
        try:
            async for ws in websockets.connect(uri):
                try:
                    async for message in ws:
                        try:
                            parsed = json.loads(message)
                            if parsed.get("type") == "response":
                                priority = 0 if parsed.get("urgent") else 1
                                notif = Notification(
                                    priority=priority,
                                    timestamp=time.time(),
                                    agent_name=self.name,
                                    agent_voice=self.voice,
                                    text=parsed.get("text", ""),
                                )
                                await queue.put(notif)
                                logger.info("Notification from %s: %s", self.name, notif.text[:80])
                        except (json.JSONDecodeError, KeyError):
                            logger.warning("Malformed WebSocket message from %s", self.name)
                except websockets.ConnectionClosed:
                    logger.warning("WebSocket closed for %s -- reconnecting", self.name)
                    continue
        except asyncio.CancelledError:
            logger.info("WebSocket listener for %s cancelled", self.name)


# Register with the router
AgentRouter.register("openclaw", OpenClawAgent)
