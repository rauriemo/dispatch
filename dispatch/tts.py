"""Edge TTS playback -- in-memory, per-agent voice.

Sentences are synthesized and played in a pipeline: the first sentence
starts playing almost immediately while subsequent sentences are generated
in the background, giving low perceived latency even for long responses.
"""

import asyncio
import logging
import re
from io import BytesIO

import edge_tts
import pygame

logger = logging.getLogger(__name__)

_SENTENCE_RE = re.compile(r'(?<=[.!?…])\s+|(?<=\n)\s*')


def _clean_for_speech(text: str) -> str:
    """Strip emoji and markdown that don't sound good in TTS."""
    text = re.sub(r'[\U0001f300-\U0001f9ff]', '', text)
    text = text.replace('**', '').replace('__', '').strip()
    return text


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences for pipelined TTS."""
    parts = _SENTENCE_RE.split(text)
    return [s.strip() for s in parts if s.strip()]


async def _synthesize(text: str, voice: str) -> BytesIO | None:
    """Generate audio for a single text chunk. Returns None on empty audio."""
    buffer = BytesIO()
    communicate = edge_tts.Communicate(text, voice)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buffer.write(chunk["data"])
    if buffer.tell() == 0:
        return None
    buffer.seek(0)
    return buffer


async def _play(buffer: BytesIO) -> None:
    """Load a BytesIO MP3 buffer into pygame and wait for playback to finish."""
    pygame.mixer.music.load(buffer, "mp3")
    pygame.mixer.music.play()
    while pygame.mixer.music.get_busy():
        await asyncio.sleep(0.05)


async def speak(text: str, voice: str) -> None:
    """Synthesize and play text sentence-by-sentence in a pipeline.

    The first sentence begins playing while the rest are synthesized in
    the background, minimizing perceived latency.
    """
    speech_text = _clean_for_speech(text)
    logger.info("TTS [%s]: %s", voice, speech_text[:120])

    sentences = _split_sentences(speech_text)
    if not sentences:
        logger.warning("TTS produced no sentences to speak")
        return

    audio_queue: asyncio.Queue[BytesIO | None] = asyncio.Queue()

    async def produce():
        for sentence in sentences:
            buf = await _synthesize(sentence, voice)
            if buf is not None:
                await audio_queue.put(buf)
        await audio_queue.put(None)

    producer = asyncio.create_task(produce())

    while True:
        buf = await audio_queue.get()
        if buf is None:
            break
        await _play(buf)

    await producer
