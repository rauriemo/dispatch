"""aiohttp.web server -- POST /notify endpoint for cron/scheduled delivery."""

import logging
import os
import time

from aiohttp import web

from dispatch.notifications import Notification, NotificationQueue

logger = logging.getLogger(__name__)


def _create_app(
    notification_queue: NotificationQueue,
    agent_voices: dict[str, str],
) -> web.Application:
    """Build the aiohttp app with the /notify route."""
    secret = os.environ.get("DISPATCH_WEBHOOK_SECRET")

    async def handle_notify(request: web.Request) -> web.Response:
        # Auth check
        if secret:
            auth = request.headers.get("Authorization", "")
            if auth != f"Bearer {secret}":
                return web.json_response(
                    {"ok": False, "error": "unauthorized"},
                    status=401,
                )

        # Parse JSON
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "invalid JSON"},
                status=400,
            )

        if not isinstance(body, dict):
            return web.json_response(
                {"ok": False, "error": "invalid JSON"},
                status=400,
            )

        # Validate required fields
        agent_name = body.get("agent")
        text = body.get("text")

        if not agent_name:
            return web.json_response(
                {"ok": False, "error": "missing required field: agent"},
                status=400,
            )
        if not text or not isinstance(text, str) or not text.strip():
            return web.json_response(
                {"ok": False, "error": "missing required field: text"},
                status=400,
            )

        # Lookup agent voice
        voice = agent_voices.get(agent_name)
        if voice is None:
            return web.json_response(
                {"ok": False, "error": "unknown agent"},
                status=404,
            )

        priority = body.get("priority", 1)

        notif = Notification(
            priority=priority,
            timestamp=time.time(),
            agent_name=agent_name,
            agent_voice=voice,
            text=text,
        )
        await notification_queue.put(notif)

        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_post("/notify", handle_notify)
    return app


class WebhookServer:
    """Manages the aiohttp runner/site lifecycle."""

    def __init__(
        self,
        notification_queue: NotificationQueue,
        agent_voices: dict[str, str],
        port: int,
    ) -> None:
        self._app = _create_app(notification_queue, agent_voices)
        self._port = port
        self._runner: web.AppRunner | None = None

    @property
    def app(self) -> web.Application:
        return self._app

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self._port)
        await site.start()
        logger.info("Webhook server listening on 127.0.0.1:%d", self._port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
