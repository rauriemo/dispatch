"""Edge TTS playback -- in-memory, per-agent voice."""

import asyncio
import logging
from io import BytesIO

import edge_tts
import pygame

logger = logging.getLogger(__name__)


async def speak(text: str, voice: str) -> None:
    """Synthesize text with edge-tts and play via pygame mixer."""
    logger.info("TTS [%s]: %s", voice, text[:120])

    buffer = BytesIO()
    communicate = edge_tts.Communicate(text, voice)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buffer.write(chunk["data"])

    if buffer.tell() == 0:
        logger.warning("TTS produced no audio data")
        return

    # CRITICAL: seek(0) before load -- cursor is at end after writing
    buffer.seek(0)

    # CRITICAL: "mp3" format parameter required for BytesIO MP3 data
    pygame.mixer.music.load(buffer, "mp3")
    pygame.mixer.music.play()

    # Poll until playback finishes
    while pygame.mixer.music.get_busy():
        await asyncio.sleep(0.05)
