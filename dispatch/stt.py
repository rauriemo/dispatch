"""Speech-to-text via Google Cloud STT streaming + debug fallback."""

import asyncio
import logging
import queue
import struct

logger = logging.getLogger(__name__)


async def stream_transcribe(frame_queue: queue.Queue) -> str:
    """Async wrapper around blocking gRPC streaming_recognize."""
    return await asyncio.to_thread(_blocking_transcribe, frame_queue)


def _blocking_transcribe(frame_queue: queue.Queue) -> str:
    """Run Google Cloud STT in a thread. Reads int16 frames from queue."""
    from google.cloud import speech

    client = speech.SpeechClient()
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=16000,
        language_code="en-US",
        enable_automatic_punctuation=True,
    )
    streaming_config = speech.StreamingRecognitionConfig(
        config=config,
        single_utterance=True,
    )

    def frame_generator():
        while True:
            try:
                frame = frame_queue.get(timeout=10)
            except queue.Empty:
                return
            # Convert list[int] (int16) to raw bytes for Google STT
            audio_bytes = struct.pack(f"<{len(frame)}h", *frame)
            yield speech.StreamingRecognizeRequest(audio_content=audio_bytes)

    try:
        responses = client.streaming_recognize(streaming_config, frame_generator())
        for response in responses:
            for result in response.results:
                if result.is_final:
                    transcript = result.alternatives[0].transcript
                    logger.info("STT transcript: %s", transcript)
                    return transcript
    except Exception:
        logger.error("STT streaming error", exc_info=True)

    return ""


async def debug_transcribe(frame_queue: queue.Queue) -> str:
    """Debug fallback: typed input instead of speech recognition."""
    text = await asyncio.to_thread(input, "Say something> ")
    return text.strip()
