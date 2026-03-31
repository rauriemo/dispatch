"""Tests for dispatch.tts -- BytesIO handling, seek(0), speak()."""

import asyncio
from io import BytesIO
from unittest.mock import patch, MagicMock, AsyncMock, call

import pytest

from dispatch.tts import speak


def _make_mock_communicate(audio_data=b"fake-mp3-data"):
    """Create a mock edge_tts.Communicate that yields audio chunks."""
    mock_comm_instance = MagicMock()

    async def mock_stream():
        yield {"type": "audio", "data": audio_data}

    mock_comm_instance.stream = mock_stream
    mock_comm_cls = MagicMock(return_value=mock_comm_instance)
    return mock_comm_cls


class TestSpeak:
    async def test_speak_calls_edge_tts_with_correct_voice(self):
        """edge_tts.Communicate should be called with correct text and voice."""
        mock_comm = _make_mock_communicate()

        with patch("dispatch.tts.edge_tts.Communicate", mock_comm):
            await speak("hello world", "en-US-GuyNeural")

        mock_comm.assert_called_once_with("hello world", "en-US-GuyNeural")

    async def test_speak_seeks_buffer_before_load(self):
        """BytesIO passed to pygame.mixer.music.load must have position 0."""
        mock_comm = _make_mock_communicate(b"fake-audio")
        captured_buffer = None

        original_load = None

        def capture_load(buf, fmt):
            nonlocal captured_buffer
            captured_buffer = buf

        with (
            patch("dispatch.tts.edge_tts.Communicate", mock_comm),
            patch("dispatch.tts.pygame.mixer.music.load", side_effect=capture_load),
            patch("dispatch.tts.pygame.mixer.music.play"),
            patch("dispatch.tts.pygame.mixer.music.get_busy", return_value=False),
        ):
            await speak("test", "en-US-GuyNeural")

        assert captured_buffer is not None
        assert captured_buffer.tell() == 0  # seek(0) was called before load... but load read it
        # Actually, we need to check that when load was called, position was 0.
        # Since our mock doesn't advance the cursor, position should still be 0
        # after seek(0). The real test: the buffer contains data and starts at 0.
        captured_buffer.seek(0)
        assert captured_buffer.read() == b"fake-audio"

    async def test_speak_loads_with_mp3_format(self):
        """pygame.mixer.music.load must be called with 'mp3' as second arg."""
        mock_comm = _make_mock_communicate()

        with (
            patch("dispatch.tts.edge_tts.Communicate", mock_comm),
            patch("dispatch.tts.pygame.mixer.music.load") as mock_load,
            patch("dispatch.tts.pygame.mixer.music.play"),
            patch("dispatch.tts.pygame.mixer.music.get_busy", return_value=False),
        ):
            await speak("test", "en-US-GuyNeural")

        mock_load.assert_called_once()
        args = mock_load.call_args
        assert args[0][1] == "mp3"

    async def test_speak_waits_for_playback(self):
        """speak() should poll get_busy() and return only after playback finishes."""
        mock_comm = _make_mock_communicate()
        busy_returns = [True, True, False]

        with (
            patch("dispatch.tts.edge_tts.Communicate", mock_comm),
            patch("dispatch.tts.pygame.mixer.music.load"),
            patch("dispatch.tts.pygame.mixer.music.play"),
            patch("dispatch.tts.pygame.mixer.music.get_busy", side_effect=busy_returns),
        ):
            await speak("test", "en-US-GuyNeural")

        # If we got here without hanging, the polling worked

    async def test_speak_empty_audio_data(self):
        """If edge-tts produces no audio, speak should return without crashing."""
        mock_comm_instance = MagicMock()

        async def mock_stream():
            # Yield no audio chunks
            return
            yield  # make it an async generator

        mock_comm_instance.stream = mock_stream
        mock_comm_cls = MagicMock(return_value=mock_comm_instance)

        with (
            patch("dispatch.tts.edge_tts.Communicate", mock_comm_cls),
            patch("dispatch.tts.pygame.mixer.music.load") as mock_load,
        ):
            await speak("", "en-US-GuyNeural")

        # load should NOT be called if no audio data
        mock_load.assert_not_called()
