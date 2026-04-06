"""TTS playback with premium provider support and Edge TTS fallback.

Supports OpenAI, ElevenLabs, Google Cloud TTS, and Edge TTS (free).
Voice format uses provider prefixes: ``openai/nova``, ``elevenlabs/Rachel``,
``google/en-US-Neural2-F``.  No prefix means Edge TTS.

Sentences are synthesized and played in a pipeline: the first sentence
starts playing almost immediately while subsequent sentences are generated
in the background, giving low perceived latency even for long responses.
"""

import asyncio
import logging
import os
import re
from io import BytesIO

import edge_tts
import pygame

logger = logging.getLogger(__name__)

_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+|(?<=\n)\s*")

_warned_providers: set[str] = set()


# -- Text cleaning / splitting ------------------------------------------------


def _clean_for_speech(text: str) -> str:
    """Strip emoji and markdown that don't sound good in TTS."""
    text = re.sub(r"[\U0001f300-\U0001f9ff]", "", text)
    text = text.replace("**", "").replace("__", "").strip()
    return text


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences for pipelined TTS."""
    parts = _SENTENCE_RE.split(text)
    return [s.strip() for s in parts if s.strip()]


# -- Voice parsing -------------------------------------------------------------


def parse_voice(voice: str) -> tuple[str, str]:
    """Split a prefixed voice string into (provider, voice_name).

    ``"openai/nova"``  -> ``("openai", "nova")``
    ``"elevenlabs/Rachel"`` -> ``("elevenlabs", "Rachel")``
    ``"google/en-US-Neural2-F"`` -> ``("google", "en-US-Neural2-F")``
    ``"edge/en-US-AvaMultilingualNeural"`` -> ``("edge", "en-US-AvaMultilingualNeural")``
    ``"en-US-AvaMultilingualNeural"`` -> ``("edge", "en-US-AvaMultilingualNeural")``
    """
    if "/" in voice:
        provider, name = voice.split("/", 1)
        provider = provider.lower()
        if provider in ("openai", "elevenlabs", "google", "edge"):
            return provider, name
    return "edge", voice


# -- Provider synthesizers -----------------------------------------------------


async def _synthesize_edge(text: str, voice_name: str) -> BytesIO | None:
    """Synthesize via Edge TTS (free, no API key)."""
    buffer = BytesIO()
    communicate = edge_tts.Communicate(text, voice_name)
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            buffer.write(chunk["data"])
    if buffer.tell() == 0:
        return None
    buffer.seek(0)
    return buffer


async def _synthesize_openai(text: str, voice_name: str) -> BytesIO | None:
    """Synthesize via OpenAI TTS API. Requires OPENAI_API_KEY."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI()
    response = await client.audio.speech.create(
        model="tts-1",
        voice=voice_name,
        input=text,
        response_format="mp3",
    )
    audio_bytes = response.content
    if not audio_bytes:
        return None
    buffer = BytesIO(audio_bytes)
    return buffer


async def _synthesize_elevenlabs(text: str, voice_name: str) -> BytesIO | None:
    """Synthesize via ElevenLabs API. Requires ELEVENLABS_API_KEY."""
    import httpx

    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY not set")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_name}",
            headers={"xi-api-key": api_key, "Content-Type": "application/json"},
            json={
                "text": text,
                "model_id": "eleven_multilingual_v2",
                "output_format": "mp3_44100_128",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        audio_bytes = resp.content

    if not audio_bytes:
        return None
    return BytesIO(audio_bytes)


async def _synthesize_google(text: str, voice_name: str) -> BytesIO | None:
    """Synthesize via Google Cloud TTS. Reuses GOOGLE_APPLICATION_CREDENTIALS."""
    from google.cloud import texttospeech

    client = texttospeech.TextToSpeechAsyncClient()
    synthesis_input = texttospeech.SynthesisInput(text=text)

    lang_code = "-".join(voice_name.split("-")[:2]) if "-" in voice_name else "en-US"

    voice_params = texttospeech.VoiceSelectionParams(
        language_code=lang_code,
        name=voice_name,
    )
    audio_config = texttospeech.AudioConfig(
        audio_encoding=texttospeech.AudioEncoding.MP3,
    )

    response = await client.synthesize_speech(
        input=synthesis_input,
        voice=voice_params,
        audio_config=audio_config,
    )

    if not response.audio_content:
        return None
    return BytesIO(response.audio_content)


# -- Synthesis dispatcher with fallback ----------------------------------------


def _get_synth_fn(provider: str):
    """Look up the synthesizer function for a provider at call time."""
    return {
        "edge": _synthesize_edge,
        "openai": _synthesize_openai,
        "elevenlabs": _synthesize_elevenlabs,
        "google": _synthesize_google,
    }.get(provider, _synthesize_edge)


async def _synthesize(
    text: str,
    voice: str,
    fallback_voice: str = "",
) -> BytesIO | None:
    """Synthesize text, falling back to Edge TTS on provider failure."""
    provider, voice_name = parse_voice(voice)

    if provider == "edge":
        return await _synthesize_edge(text, voice_name)

    synth_fn = _get_synth_fn(provider)
    try:
        buf = await synth_fn(text, voice_name)
        if buf is not None:
            return buf
    except (ModuleNotFoundError, RuntimeError) as exc:
        if provider not in _warned_providers:
            _warned_providers.add(provider)
            logger.warning("TTS provider '%s' unavailable: %s -- using Edge TTS", provider, exc)
    except Exception as exc:
        if provider not in _warned_providers:
            _warned_providers.add(provider)
            logger.warning("TTS provider '%s' failed: %s -- using Edge TTS", provider, exc)

    _, fb_name = parse_voice(fallback_voice) if fallback_voice else ("edge", voice_name)
    return await _synthesize_edge(text, fb_name)


# -- Playback ------------------------------------------------------------------


async def _play(buffer: BytesIO) -> None:
    """Load a BytesIO MP3 buffer into pygame and wait for playback to finish."""
    pygame.mixer.music.load(buffer, "mp3")
    pygame.mixer.music.play()
    while pygame.mixer.music.get_busy():
        await asyncio.sleep(0.05)


# -- Public API ----------------------------------------------------------------


async def speak(text: str, voice: str, fallback_voice: str = "") -> None:
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
            buf = await _synthesize(sentence, voice, fallback_voice)
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
