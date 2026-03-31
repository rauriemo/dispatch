"""Audio pipeline -- single pvrecorder capture with state machine + debug fallback."""

import array
import asyncio
import enum
import logging
import math
import queue
import threading
from typing import Optional

import pygame

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
CHIME_FREQ = 880
CHIME_DURATION_MS = 150
MIXER_RATE = 44100


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


class DebugPipeline:
    """Keyboard-based fallback when Picovoice is unavailable.

    Same interface as AudioPipeline so main.py doesn't branch.
    Uses a persistent input thread + asyncio.Event so listen() respects
    timeouts and the main loop can drain notifications between cycles.
    """

    def __init__(self, config) -> None:
        self.frame_queue: queue.Queue = queue.Queue()
        self._chime = _generate_chime()
        self._input_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._wake_event: Optional[asyncio.Event] = None

    def _input_loop(self) -> None:
        """Background thread: waits for Enter key presses."""
        while True:
            try:
                input("Press Enter for wake word> ")
            except EOFError:
                break
            if self._loop and self._wake_event:
                self._loop.call_soon_threadsafe(self._wake_event.set)

    async def listen(self, timeout: float = 2.0) -> Optional[int]:
        """Wait for Enter key press with timeout. Returns 0 or None."""
        if self._input_thread is None:
            self._loop = asyncio.get_running_loop()
            self._wake_event = asyncio.Event()
            self._input_thread = threading.Thread(target=self._input_loop, daemon=True)
            self._input_thread.start()

        self._wake_event.clear()
        try:
            await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
            self._chime.play()
            return 0
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
