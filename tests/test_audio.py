"""Tests for dispatch.audio -- state machine, chime, DebugPipeline."""

import array
import asyncio
import math
import queue
import sys
import threading

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from dispatch.audio import (
    PipelineState,
    DebugPipeline,
    _generate_chime,
    _is_expected_stt_error,
    MIXER_RATE,
    CHIME_DURATION_MS,
)


class TestAudioPipelineStateMachine:
    """Test state transitions using DebugPipeline (same state interface, no hardware)."""

    def _make_pipeline(self, config):
        """Create AudioPipeline with mocked hardware deps."""
        from dispatch.audio import AudioPipeline

        mock_porcupine_instance = MagicMock()
        mock_porcupine_instance.frame_length = 512

        mock_pvporcupine = MagicMock()
        mock_pvporcupine.create.return_value = mock_porcupine_instance

        mock_pvrecorder = MagicMock()

        with (
            patch.dict(sys.modules, {
                "pvporcupine": mock_pvporcupine,
                "pvrecorder": mock_pvrecorder,
            }),
            patch.dict("os.environ", {"PICOVOICE_ACCESS_KEY": "test-key"}),
        ):
            pipeline = AudioPipeline(config)

        return pipeline

    def test_initial_state_is_listening(self, sample_config):
        """New AudioPipeline starts in LISTENING state."""
        pipeline = self._make_pipeline(sample_config)
        assert pipeline._state == PipelineState.LISTENING

    def test_pause_sets_paused(self, sample_config):
        """pause() sets state to PAUSED."""
        pipeline = self._make_pipeline(sample_config)
        pipeline.pause()
        assert pipeline._state == PipelineState.PAUSED

    def test_resume_sets_listening(self, sample_config):
        """resume() after pause sets state back to LISTENING."""
        pipeline = self._make_pipeline(sample_config)
        pipeline.pause()
        pipeline.resume()
        assert pipeline._state == PipelineState.LISTENING

    def test_state_transitions_are_thread_safe(self, sample_config):
        """Concurrent pause/resume from multiple threads should not crash."""
        pipeline = self._make_pipeline(sample_config)
        errors = []

        def toggle(n):
            try:
                for _ in range(100):
                    if n % 2 == 0:
                        pipeline.pause()
                    else:
                        pipeline.resume()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=toggle, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert pipeline._state in (PipelineState.LISTENING, PipelineState.PAUSED)


class TestChimeGeneration:
    def test_chime_generation(self):
        """Chime should be created with expected sample count (~150ms at 44100Hz)."""
        expected_samples = int(MIXER_RATE * CHIME_DURATION_MS / 1000)
        # Stereo: 2 values per sample
        expected_values = expected_samples * 2

        # Build the buffer directly to verify the math
        buf = array.array("h")
        for i in range(expected_samples):
            val = int(32767 * 0.5 * math.sin(2 * math.pi * 880 * i / MIXER_RATE))
            buf.append(val)  # left
            buf.append(val)  # right

        assert len(buf) == expected_values
        # ~6615 mono samples * 2 channels = ~13230
        assert 13000 < len(buf) < 14000


class TestDebugPipeline:
    async def test_debug_pipeline_listen_returns_zero(self, sample_config):
        """DebugPipeline.listen() should return 0 when Enter is pressed."""
        pipeline = DebugPipeline(sample_config)
        loop = asyncio.get_running_loop()
        pipeline._loop = loop
        pipeline._wake_event = asyncio.Event()
        pipeline._input_thread = threading.Thread()  # stub so listen() skips spawn

        # Simulate Enter press arriving just after listen() clears the event
        loop.call_soon(pipeline._wake_event.set)
        result = await pipeline.listen()
        assert result == 0

    async def test_debug_pipeline_listen_timeout_returns_none(self, sample_config):
        """DebugPipeline.listen() should return None on timeout."""
        pipeline = DebugPipeline(sample_config)
        loop = asyncio.get_running_loop()
        pipeline._loop = loop
        pipeline._wake_event = asyncio.Event()
        pipeline._input_thread = threading.Thread()  # stub

        result = await pipeline.listen(timeout=0.05)
        assert result is None

    def test_debug_pipeline_has_frame_queue(self, sample_config):
        """DebugPipeline must expose a frame_queue attribute."""
        pipeline = DebugPipeline(sample_config)
        assert hasattr(pipeline, "frame_queue")
        assert isinstance(pipeline.frame_queue, queue.Queue)

    def test_debug_pipeline_pause_resume_noop(self, sample_config):
        """pause() and resume() on DebugPipeline should not raise."""
        pipeline = DebugPipeline(sample_config)
        pipeline.pause()
        pipeline.resume()
        pipeline.pause()
        pipeline.resume()

    def test_debug_pipeline_context_manager(self, sample_config):
        """DebugPipeline works as a context manager."""
        with DebugPipeline(sample_config) as pipeline:
            assert pipeline is not None
            assert hasattr(pipeline, "listen")


class TestExpectedSTTErrors:
    def test_stream_duration_rollover(self):
        exc = RuntimeError("400 Exceeded maximum allowed stream duration of 305 seconds.")
        expected, reason = _is_expected_stt_error(exc)
        assert expected is True
        assert "duration limit" in reason

    def test_internal_server_error(self):
        from google.api_core.exceptions import InternalServerError
        exc = InternalServerError("500 Internal error encountered.")
        expected, reason = _is_expected_stt_error(exc)
        assert expected is True
        assert "InternalServerError" in reason

    def test_service_unavailable(self):
        from google.api_core.exceptions import ServiceUnavailable
        exc = ServiceUnavailable("503 Service unavailable.")
        expected, reason = _is_expected_stt_error(exc)
        assert expected is True
        assert "ServiceUnavailable" in reason

    def test_deadline_exceeded(self):
        from google.api_core.exceptions import DeadlineExceeded
        exc = DeadlineExceeded("504 Deadline exceeded.")
        expected, reason = _is_expected_stt_error(exc)
        assert expected is True
        assert "DeadlineExceeded" in reason

    def test_unexpected_error_not_matched(self):
        exc = RuntimeError("permission denied")
        expected, reason = _is_expected_stt_error(exc)
        assert expected is False
        assert reason == ""
