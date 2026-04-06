"""Main entry -- hotkey toggle, system tray, main voice-command loop."""

import asyncio
import logging
import os
import threading

import pygame
from PIL import Image, ImageDraw
from pynput.keyboard import GlobalHotKeys

from dispatch.agents import AgentError, AgentRouter
from dispatch.audio import AudioPipeline, DebugPipeline, PipelineState, STTWakePipeline
from dispatch.config import load_config
from dispatch.notifications import NotificationQueue
from dispatch.stt import debug_transcribe, stream_transcribe
from dispatch.tts import speak
from dispatch.webhook import WebhookServer

logger = logging.getLogger(__name__)

_SENTENCE_ENDERS = ".!?"
_LATEST_VARIANTS = ("latest", "whats latest")
_LATEST_PROMPT = "In 1-2 sentences, what is the last thing you worked on or did?"


def _limit_to_n_sentences(text: str, n: int) -> str:
    """Clamp *text* to the first *n* sentences.

    Only splits on sentence-ending punctuation followed by whitespace or
    end-of-string, so abbreviations like ``v2.1`` are not treated as
    sentence boundaries.
    """
    text = text.strip()
    count = 0
    for i, ch in enumerate(text):
        if ch in _SENTENCE_ENDERS and i > 0:
            next_ch = text[i + 1] if i + 1 < len(text) else " "
            if next_ch in (" ", "\n") or i + 1 == len(text):
                count += 1
                if count >= n:
                    return text[: i + 1]
    return text


def _limit_to_one_sentence(text: str) -> str:
    """Backward-compatible wrapper -- clamp to first sentence."""
    return _limit_to_n_sentences(text, 1)


def _is_latest_keyword(transcript: str) -> bool:
    """Substring match for 'latest' variants.

    Handles STT filler like 'the latest', 'what's the latest'.
    """
    normalized = transcript.strip().lower()
    return any(v in normalized for v in _LATEST_VARIANTS)


# ── System tray ──────────────────────────────────────────────────────


def _make_icon(color: str) -> Image.Image:
    """Generate a 64x64 image with a filled circle for the tray icon."""
    img = Image.new("RGB", (64, 64), "black")
    draw = ImageDraw.Draw(img)
    draw.ellipse((8, 8, 56, 56), fill=color)
    return img


def start_tray(pipeline, toggle_fn, quit_fn):
    """Launch pystray icon in a daemon thread."""
    import pystray

    icon = pystray.Icon(
        "dispatch",
        _make_icon("green"),
        "Dispatch",
        menu=pystray.Menu(
            pystray.MenuItem("Toggle", lambda: toggle_fn()),
            pystray.MenuItem("Quit", lambda: quit_fn()),
        ),
    )

    def _run():
        icon.run()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return icon


# ── Hotkey ───────────────────────────────────────────────────────────


def start_hotkey(hotkey_str: str, callback):
    """Start pynput GlobalHotKeys listener (angle-bracket format required)."""
    hotkeys = GlobalHotKeys({hotkey_str: callback})
    hotkeys.start()
    logger.info("Global hotkey registered: %s", hotkey_str)
    return hotkeys


# ── Main ─────────────────────────────────────────────────────────────


def main(debug: bool = False) -> None:
    config = load_config(debug)
    logger.info("Dispatch starting (debug=%s)", debug)

    # Init pygame mixer: stereo for edge-tts MP3
    pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)

    # Three-tier pipeline selection: Picovoice -> STT wake -> Debug (keyboard)
    has_picovoice = bool(os.environ.get("PICOVOICE_ACCESS_KEY"))
    has_google = bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))

    if debug:
        pipeline_cls = DebugPipeline
        transcribe_fn = debug_transcribe
    elif has_picovoice:
        pipeline_cls = AudioPipeline
        transcribe_fn = stream_transcribe
    elif has_google:
        pipeline_cls = STTWakePipeline
        transcribe_fn = stream_transcribe
    else:
        pipeline_cls = DebugPipeline
        transcribe_fn = debug_transcribe

    async def run():
        loop = asyncio.get_running_loop()
        active = True
        tray_icon = None

        async with AgentRouter(config.agents) as router:
            agent_fallbacks = {ac.name: ac.fallback_voice for ac in config.agents}
            notification_queue = NotificationQueue()
            for agent in router.agents:
                await agent.subscribe(notification_queue)

            # Webhook server for scheduled cron delivery
            webhook_server = None
            if config.webhook_port > 0:
                agent_voices = {a.name: a.voice for a in router.agents}
                webhook_server = WebhookServer(
                    notification_queue,
                    agent_voices,
                    config.webhook_port,
                )
                try:
                    await webhook_server.start()
                except OSError:
                    logger.warning(
                        "Webhook server failed to bind on port %d -- scheduled delivery unavailable",
                        config.webhook_port,
                    )
                    webhook_server = None

            broadcast_index = len(config.agents)

            try:
                if pipeline_cls is STTWakePipeline:
                    wake_phrases = [(a.wake_phrase, i) for i, a in enumerate(config.agents)]
                    wake_phrases.append((config.broadcast_wake_phrase, broadcast_index))
                    pipeline_ctx = pipeline_cls(config, wake_phrases)
                else:
                    pipeline_ctx = pipeline_cls(config)
                with pipeline_ctx as pipeline:
                    # Toggle callback (called from hotkey thread)
                    def on_toggle():
                        nonlocal active
                        active = not active
                        if active:
                            pipeline.resume()
                            logger.info("Dispatch resumed")
                            if tray_icon:
                                tray_icon.icon = _make_icon("green")
                        else:
                            pipeline.pause()
                            logger.info("Dispatch paused")
                            if tray_icon:
                                tray_icon.icon = _make_icon("gray")

                    def on_quit():
                        logger.info("Quit requested from tray")
                        loop.call_soon_threadsafe(loop.stop)

                    tray_icon = start_tray(pipeline, on_toggle, on_quit)
                    start_hotkey(config.hotkey, lambda: loop.call_soon_threadsafe(on_toggle))

                    logger.info("Dispatch ready -- listening for wake words")

                    while True:
                        # 1. Drain notification queue (non-blocking)
                        while not notification_queue.empty():
                            try:
                                notif = notification_queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            pipeline.pause()
                            notif_fb = agent_fallbacks.get(notif.agent_name, "")
                            await speak(
                                f"{notif.agent_name} says: {notif.text}",
                                notif.agent_voice,
                                notif_fb,
                            )
                            pipeline.resume()

                        # 2. Listen for wake words (2s timeout so notifications get checked)
                        if not active:
                            await asyncio.sleep(0.5)
                            continue

                        keyword_index = await pipeline.listen(timeout=2.0)
                        if keyword_index is None:
                            continue

                        # 3. Transcribe speech (may already be captured in single-utterance mode)
                        pending = getattr(pipeline, "pending_command", None)
                        if pending:
                            transcript = pending
                            pipeline.pending_command = None
                            pipeline.set_state(PipelineState.LISTENING)
                        else:
                            transcript = await transcribe_fn(pipeline.frame_queue)
                            pipeline.set_state(PipelineState.LISTENING)

                        if keyword_index == broadcast_index:
                            # ── Broadcast mode: all agents ──
                            logger.info("Broadcast mode triggered")

                            # "checkin" shortcut -- each agent announces itself
                            if transcript and transcript.strip().lower() in (
                                "checkin",
                                "check in",
                                "checking in",
                            ):
                                logger.info("Broadcast checkin")
                                pipeline.pause()
                                for agent in router.agents:
                                    fb = agent_fallbacks.get(agent.name, "")
                                    await speak(
                                        f"{agent.name} checking in",
                                        agent.voice,
                                        fb,
                                    )
                                pipeline.resume()
                                continue

                            # "latest" keyword -- ask each agent for a quick status
                            if transcript and _is_latest_keyword(transcript):
                                logger.info("Broadcast latest")
                                tasks = [a.send(_LATEST_PROMPT) for a in router.agents]
                                results = await asyncio.gather(
                                    *tasks,
                                    return_exceptions=True,
                                )
                                pipeline.pause()
                                for agent, result in zip(router.agents, results):
                                    fb = agent_fallbacks.get(agent.name, "")
                                    if isinstance(result, Exception):
                                        logger.error(
                                            "Agent '%s' failed in broadcast latest",
                                            agent.name,
                                            exc_info=result,
                                        )
                                        text = f"{agent.name} is not responding"
                                    else:
                                        text = f"{agent.name} says: {_limit_to_n_sentences(result, 2)}"
                                    await speak(text, agent.voice, fb)
                                pipeline.resume()
                                continue

                            if not transcript:
                                logger.info("Empty transcript, returning to listening")
                                continue

                            logger.info("Broadcast transcript: %s", transcript)

                            # Fan out to all agents with 1-sentence limit
                            prompt = "Respond in exactly one sentence. " + transcript
                            tasks = [a.send(prompt) for a in router.agents]
                            results = await asyncio.gather(
                                *tasks,
                                return_exceptions=True,
                            )

                            pipeline.pause()
                            for agent, result in zip(router.agents, results):
                                fb = agent_fallbacks.get(agent.name, "")
                                if isinstance(result, Exception):
                                    logger.error(
                                        "Agent '%s' failed in broadcast",
                                        agent.name,
                                        exc_info=result,
                                    )
                                    text = f"{agent.name} is not responding"
                                else:
                                    text = f"{agent.name} says: {_limit_to_one_sentence(result)}"
                                await speak(text, agent.voice, fb)
                            pipeline.resume()
                        else:
                            # ── Single-agent mode ──
                            agent = router.route(keyword_index)
                            logger.info("Routing to agent '%s'", agent.name)

                            if not transcript:
                                logger.info("Empty transcript, returning to listening")
                                continue

                            # "latest" keyword -- quick status from this agent
                            if _is_latest_keyword(transcript):
                                logger.info("Latest keyword detected for agent '%s'", agent.name)
                                try:
                                    response = await agent.send(_LATEST_PROMPT)
                                except AgentError:
                                    logger.error("Agent '%s' failed", agent.name, exc_info=True)
                                    response = f"{agent.name} is not responding"
                                response = _limit_to_n_sentences(response, 2)
                                pipeline.pause()
                                fb = agent_fallbacks.get(agent.name, "")
                                await speak(response, agent.voice, fb)
                                pipeline.resume()
                                continue

                            logger.info("Transcript: %s", transcript)

                            # Send to agent
                            try:
                                response = await agent.send(transcript)
                            except AgentError:
                                logger.error("Agent '%s' failed", agent.name, exc_info=True)
                                response = f"{agent.name} is not responding"

                            # Speak response
                            pipeline.pause()
                            fb = agent_fallbacks.get(agent.name, "")
                            await speak(response, agent.voice, fb)
                            pipeline.resume()
            finally:
                if webhook_server:
                    await webhook_server.stop()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("Dispatch shutting down")
    finally:
        pygame.mixer.quit()
