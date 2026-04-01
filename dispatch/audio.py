"""Audio pipeline -- single pvrecorder capture with state machine + debug fallback."""

import array
import asyncio
import enum
import logging
import math
import queue
import struct
import threading
import time
from typing import Optional

import pygame

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHIME_FREQ = 880
CHIME_DURATION_MS = 150
MIXER_RATE = 44100


def _is_expected_stt_error(exc: Exception) -> tuple[bool, str]:
    """Classify expected/transient STT errors that should log cleanly.

    Returns ``(True, reason)`` for errors that are routine and recoverable,
    ``(False, "")`` for anything unexpected that deserves a full traceback.
    """
    msg = str(exc)
    if "Exceeded maximum allowed stream duration" in msg:
        return True, "stream duration limit reached"
    from google.api_core import exceptions as gexc
    if isinstance(exc, (gexc.InternalServerError, gexc.ServiceUnavailable, gexc.DeadlineExceeded)):
        return True, f"transient server error ({type(exc).__name__})"
    return False, ""


class PipelineState(enum.Enum):
    LISTENING = "listening"
    RECORDING = "recording"
    PAUSED = "paused"


def _generate_chime() -> pygame.mixer.Sound:
    """Generate a 150ms 880Hz sine wave chime using stdlib only."""
    num_samples = int(MIXER_RATE * CHIME_DURATION_MS / 1000)
    # Stereo: duplicate each sample for L+R channels
    buf = array.array("h")
    for i in range(num_samples):
        val = int(32767 * 0.5 * math.sin(2 * math.pi * CHIME_FREQ * i / MIXER_RATE))
        buf.append(val)  # left
        buf.append(val)  # right
    return pygame.mixer.Sound(buffer=buf)


class AudioPipeline:
    """Single pvrecorder instance with state-machine frame routing.

    Sync context manager: __enter__ starts capture thread, __exit__ stops it.
    """

    def __init__(self, config) -> None:
        import pvporcupine
        import pvrecorder
        import os

        from dispatch.config import PROJECT_ROOT

        access_key = os.environ["PICOVOICE_ACCESS_KEY"]
        ppn_paths = [
            str(PROJECT_ROOT / agent_cfg.wake_word)
            for agent_cfg in config.agents
        ]

        self._porcupine = pvporcupine.create(
            access_key=access_key,
            keyword_paths=ppn_paths,
        )
        self._recorder = pvrecorder.PvRecorder(
            frame_length=self._porcupine.frame_length,
            device_index=config.audio_device,
        )

        self.frame_queue: queue.Queue = queue.Queue()
        self._state = PipelineState.LISTENING
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake_event: Optional[asyncio.Event] = None
        self._keyword_index: int = -1
        self._thread: Optional[threading.Thread] = None
        self._chime = _generate_chime()

    def _capture_loop(self) -> None:
        """Background thread: reads frames and routes by state."""
        self._recorder.start()
        logger.info("Audio capture started (device=%d)", self._recorder.selected_device)

        while not self._stop_event.is_set():
            try:
                frame = self._recorder.read()
            except Exception:
                logger.error("pvrecorder read error", exc_info=True)
                break

            with self._lock:
                state = self._state

            if state == PipelineState.LISTENING:
                result = self._porcupine.process(frame)
                if result >= 0:
                    logger.info("Wake word detected (keyword_index=%d)", result)
                    self._chime.play()
                    self._keyword_index = result
                    with self._lock:
                        self._state = PipelineState.RECORDING
                    if self._loop and self._wake_event:
                        self._loop.call_soon_threadsafe(self._wake_event.set)

            elif state == PipelineState.RECORDING:
                self.frame_queue.put(frame)

            # PAUSED: discard frame (keep reading to avoid buffer overflow)

        self._recorder.stop()

    async def listen(self, timeout: float = 2.0) -> Optional[int]:
        """Await wake word detection. Returns keyword index or None on timeout."""
        self._loop = asyncio.get_running_loop()
        self._wake_event = asyncio.Event()

        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
            return self._keyword_index
        except asyncio.TimeoutError:
            return None

    def set_state(self, state: PipelineState) -> None:
        with self._lock:
            old = self._state
            self._state = state
        logger.debug("Pipeline state: %s -> %s", old.value, state.value)

    def pause(self) -> None:
        self.set_state(PipelineState.PAUSED)

    def resume(self) -> None:
        self.set_state(PipelineState.LISTENING)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._porcupine.delete()
        logger.info("Audio pipeline stopped")

    def __enter__(self) -> "AudioPipeline":
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()


_STT_FRAME_LENGTH = 512


class STTWakePipeline:
    """Voice-based wake word detection using Google Cloud STT.

    Middle tier between AudioPipeline (Picovoice, local) and DebugPipeline
    (keyboard). Uses pvrecorder for mic capture and Google STT to transcribe
    speech, then matches wake phrases in the transcript text.

    Supports single-utterance: "hey navi what's the weather" returns the
    keyword index AND stores the command portion in pending_command so the
    main loop can skip a second STT call.
    """

    def __init__(self, config, wake_phrases: list[tuple[str, int]]) -> None:
        import pvrecorder as _pvrecorder

        self._recorder = _pvrecorder.PvRecorder(
            frame_length=_STT_FRAME_LENGTH,
            device_index=config.audio_device,
        )
        self._wake_phrases = wake_phrases
        self.frame_queue: queue.Queue = queue.Queue()
        self.pending_command: Optional[str] = None
        self._state = PipelineState.LISTENING
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake_event: Optional[asyncio.Event] = None
        self._keyword_index: int = -1
        self._thread: Optional[threading.Thread] = None
        self._chime = _generate_chime()

    # -- wake phrase matching --------------------------------------------------

    @staticmethod
    def _normalize(text: str) -> str:
        """Strip punctuation and smart quotes so 'na\u2019vi' matches 'navi'."""
        import re
        text = text.replace("\u2019", "").replace("\u2018", "")
        text = text.replace("'", "").replace("\u2032", "")
        text = re.sub(r"[^\w\s]", " ", text)
        return " ".join(text.lower().split())

    @staticmethod
    def _words_similar(a: str, b: str) -> bool:
        """Check if two words are similar enough (handles STT misheard variants)."""
        if a == b:
            return True
        if len(a) < 3 or len(b) < 3:
            return a == b
        from difflib import SequenceMatcher
        return SequenceMatcher(None, a, b).ratio() >= 0.65

    def _match_wake_phrase(self, transcript: str) -> tuple[Optional[int], Optional[str]]:
        """Check transcript for a wake phrase. Returns (index, command) or (None, None).

        Tries exact substring match first, then falls back to fuzzy word-level
        matching to handle STT variants like 'naive' for 'navi'.
        """
        normalized = self._normalize(transcript)
        for phrase, index in self._wake_phrases:
            norm_phrase = self._normalize(phrase)
            # Exact substring match
            pos = normalized.find(norm_phrase)
            if pos != -1:
                after = normalized[pos + len(norm_phrase):].strip()
                return (index, after if after else None)
            # Fuzzy word-level match
            phrase_words = norm_phrase.split()
            trans_words = normalized.split()
            for i in range(len(trans_words) - len(phrase_words) + 1):
                window = trans_words[i:i + len(phrase_words)]
                if all(self._words_similar(w, p) for w, p in zip(window, phrase_words)):
                    rest = " ".join(trans_words[i + len(phrase_words):])
                    return (index, rest if rest else None)
        return (None, None)

    # -- background thread -----------------------------------------------------

    def _stt_watch_loop(self) -> None:
        """Background thread: capture audio, stream to Google STT, match wake phrases."""
        try:
            self._stt_watch_loop_inner()
        except Exception:
            logger.error("STT wake thread crashed", exc_info=True)

    def _stt_watch_loop_inner(self) -> None:
        from google.cloud import speech

        self._recorder.start()
        logger.info(
            "STT wake pipeline started (device=%s, phrases=%s)",
            self._recorder.selected_device,
            [p for p, _ in self._wake_phrases],
        )

        backoff = 1
        while not self._stop_event.is_set():
            with self._lock:
                state = self._state

            if state == PipelineState.PAUSED:
                self._recorder.read()
                continue

            if state == PipelineState.RECORDING:
                frame = self._recorder.read()
                self.frame_queue.put(frame)
                continue

            # LISTENING: run continuous STT stream, check each result for wake phrase
            try:
                logger.debug("Starting STT wake stream...")
                matched = self._run_stt_stream()
                backoff = 1
            except Exception as exc:
                expected, reason = _is_expected_stt_error(exc)
                if expected:
                    logger.info("STT wake stream interrupted: %s, restarting", reason)
                    backoff = 1
                    continue
                logger.warning("STT wake stream error, retrying in %ds", backoff, exc_info=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue

            if not matched:
                logger.debug("STT wake stream ended with no match")
                continue

        self._recorder.stop()

    def _run_stt_stream(self) -> bool:
        """Run a continuous STT stream, checking each final result for wake phrases.

        Returns True if a wake phrase was matched (sets _keyword_index / pending_command),
        False if the stream ended without a match.
        """
        from google.cloud import speech

        if not hasattr(self, "_stt_client"):
            self._stt_client = speech.SpeechClient()
        client = self._stt_client
        hint_phrases = [p for p, _ in self._wake_phrases]
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=SAMPLE_RATE,
            language_code="en-US",
            enable_automatic_punctuation=True,
            speech_contexts=[speech.SpeechContext(
                phrases=hint_phrases,
                boost=20.0,
            )],
        )
        streaming_config = speech.StreamingRecognitionConfig(
            config=config,
            interim_results=True,
        )

        frames_sent = 0

        def frame_generator():
            nonlocal frames_sent
            while not self._stop_event.is_set():
                with self._lock:
                    state = self._state
                if state != PipelineState.LISTENING:
                    return
                try:
                    frame = self._recorder.read()
                except Exception:
                    return
                audio_bytes = array.array("h", frame).tobytes()
                peak = max(abs(s) for s in frame)
                frames_sent += 1
                if frames_sent == 1:
                    logger.debug("First audio frame sent to STT (%d bytes, peak=%d)", len(audio_bytes), peak)
                elif frames_sent % 500 == 0:
                    logger.debug("STT stream alive: %d frames sent (~%.0fs audio, peak=%d)", frames_sent, frames_sent * _STT_FRAME_LENGTH / SAMPLE_RATE, peak)
                elif peak > 2000 and frames_sent % 50 == 0:
                    logger.debug("Audio activity detected (peak=%d, frame=%d)", peak, frames_sent)
                yield speech.StreamingRecognizeRequest(audio_content=audio_bytes)

        responses = client.streaming_recognize(streaming_config, frame_generator())
        for response in responses:
            logger.debug("STT response: results=%d, speech_event=%s", len(response.results), response.speech_event_type)
            for result in response.results:
                if result.is_final:
                    transcript = result.alternatives[0].transcript
                    logger.debug("STT heard: '%s'", transcript)
                    keyword_index, command = self._match_wake_phrase(transcript)
                    if keyword_index is not None:
                        logger.info("Wake phrase detected: '%s' (index=%d)", transcript, keyword_index)
                        self._chime.play()
                        self._keyword_index = keyword_index
                        self.pending_command = command
                        with self._lock:
                            self._state = PipelineState.RECORDING
                        if self._loop and self._wake_event:
                            self._loop.call_soon_threadsafe(self._wake_event.set)
                        return True
                elif result.alternatives:
                    logger.debug("STT interim: '%s'", result.alternatives[0].transcript)
        return False

    # -- async interface (same as AudioPipeline) --------------------------------

    async def listen(self, timeout: float = 2.0) -> Optional[int]:
        """Await wake phrase detection. Returns keyword index or None on timeout."""
        self._loop = asyncio.get_running_loop()
        if self._wake_event is None:
            self._wake_event = asyncio.Event()
        self._wake_event.clear()

        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
            return self._keyword_index
        except asyncio.TimeoutError:
            return None

    def set_state(self, state: PipelineState) -> None:
        with self._lock:
            old = self._state
            self._state = state
        logger.debug("STTWakePipeline state: %s -> %s", old.value, state.value)

    def pause(self) -> None:
        self.set_state(PipelineState.PAUSED)

    def resume(self) -> None:
        self.set_state(PipelineState.LISTENING)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info("STT wake pipeline stopped")

    def __enter__(self) -> "STTWakePipeline":
        self._thread = threading.Thread(target=self._stt_watch_loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()


class DebugPipeline:
    """Keyboard-based fallback when Picovoice is unavailable.

    Same interface as AudioPipeline so main.py doesn't branch.
    Uses a persistent input thread + asyncio.Event so listen() respects
    timeouts and the main loop can drain notifications between cycles.

    Supports multi-agent wake phrase selection: the user types a wake phrase
    (e.g. "hey navi" or "hey anthem") and the pipeline matches it to the
    correct keyword index. The command is collected in the same thread to
    avoid stdin races with debug_transcribe, then exposed via pending_command.
    """

    def __init__(self, config) -> None:
        self.frame_queue: queue.Queue = queue.Queue()
        self.pending_command: Optional[str] = None
        self._chime = _generate_chime()
        self._input_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake_event: Optional[asyncio.Event] = None
        self._matched_index: int = 0
        self._wake_phrases: list[str] = []
        if hasattr(config, "agents"):
            self._wake_phrases = [a.wake_phrase.lower() for a in config.agents]
        broadcast = getattr(config, "broadcast_wake_phrase", "")
        if broadcast:
            self._wake_phrases.append(broadcast.lower())

    def _match_wake_phrase(self, text: str) -> int:
        """Match typed text against configured wake phrases. Returns keyword index."""
        text = text.strip().lower()
        if not text:
            return 0
        for i, phrase in enumerate(self._wake_phrases):
            if phrase in text or text in phrase:
                return i
        return 0

    def _input_loop(self) -> None:
        """Background thread: collects wake phrase + command in one flow."""
        hints = ", ".join(f'"{p}"' for p in self._wake_phrases) if self._wake_phrases else "Enter"
        while True:
            try:
                line = input(f"Wake phrase ({hints})> ")
            except EOFError:
                break
            self._matched_index = self._match_wake_phrase(line)
            try:
                cmd = input("Say something> ")
                self.pending_command = cmd.strip() or None
            except EOFError:
                self.pending_command = None
            if self._loop and self._wake_event:
                self._loop.call_soon_threadsafe(self._wake_event.set)

    async def listen(self, timeout: float = 2.0) -> Optional[int]:
        """Wait for wake phrase + command input. Returns keyword index or None."""
        if self._input_thread is None:
            self._loop = asyncio.get_running_loop()
            self._wake_event = asyncio.Event()
            self._input_thread = threading.Thread(target=self._input_loop, daemon=True)
            self._input_thread.start()

        self._wake_event.clear()
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
            self._chime.play()
            return self._matched_index
        except asyncio.TimeoutError:
            return None

    def set_state(self, state: PipelineState) -> None:
        logger.debug("DebugPipeline state: %s (no-op)", state.value)

    def pause(self) -> None:
        pass

    def resume(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def __enter__(self) -> "DebugPipeline":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()
