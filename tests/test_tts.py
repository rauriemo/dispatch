"""Tests for dispatch.tts -- voice parsing, provider routing, fallback, playback."""

import asyncio
from io import BytesIO
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from dispatch.tts import speak, parse_voice, _clean_for_speech, _split_sentences


# -- Voice parsing -------------------------------------------------------------

class TestParseVoice:
    def test_openai_prefix(self):
        assert parse_voice("openai/nova") == ("openai", "nova")

    def test_elevenlabs_prefix(self):
        assert parse_voice("elevenlabs/Rachel") == ("elevenlabs", "Rachel")

    def test_google_prefix(self):
        assert parse_voice("google/en-US-Neural2-F") == ("google", "en-US-Neural2-F")

    def test_edge_prefix(self):
        assert parse_voice("edge/en-US-AvaMultilingualNeural") == ("edge", "en-US-AvaMultilingualNeural")

    def test_no_prefix_defaults_to_edge(self):
        assert parse_voice("en-US-AvaMultilingualNeural") == ("edge", "en-US-AvaMultilingualNeural")

    def test_unknown_prefix_defaults_to_edge(self):
        assert parse_voice("unknown/voice") == ("edge", "unknown/voice")

    def test_empty_string(self):
        assert parse_voice("") == ("edge", "")

    def test_case_insensitive_provider(self):
        assert parse_voice("OpenAI/nova") == ("openai", "nova")


# -- Text helpers --------------------------------------------------------------

class TestCleanForSpeech:
    def test_strips_emoji(self):
        assert _clean_for_speech("Hello 🧭 world") == "Hello  world"

    def test_strips_markdown_bold(self):
        assert _clean_for_speech("**bold** text") == "bold text"

    def test_strips_markdown_underline(self):
        assert _clean_for_speech("__underline__") == "underline"


class TestSplitSentences:
    def test_simple_sentences(self):
        assert _split_sentences("Hello. World!") == ["Hello.", "World!"]

    def test_single_sentence(self):
        assert _split_sentences("Hello world") == ["Hello world"]

    def test_newline_split(self):
        assert _split_sentences("Line one\nLine two") == ["Line one", "Line two"]

    def test_empty_string(self):
        assert _split_sentences("") == []


# -- Provider routing ----------------------------------------------------------

def _make_mock_communicate(audio_data=b"fake-mp3-data"):
    """Create a mock edge_tts.Communicate that yields audio chunks."""
    mock_comm_instance = MagicMock()

    async def mock_stream():
        yield {"type": "audio", "data": audio_data}

    mock_comm_instance.stream = mock_stream
    mock_comm_cls = MagicMock(return_value=mock_comm_instance)
    return mock_comm_cls


class TestEdgeTTSRoute:
    async def test_speak_calls_edge_tts_with_correct_voice(self):
        mock_comm = _make_mock_communicate()

        with patch("dispatch.tts.edge_tts.Communicate", mock_comm):
            await speak("hello world", "en-US-GuyNeural")

        mock_comm.assert_called_once_with("hello world", "en-US-GuyNeural")

    async def test_edge_prefix_routes_to_edge(self):
        mock_comm = _make_mock_communicate()

        with patch("dispatch.tts.edge_tts.Communicate", mock_comm):
            await speak("hello", "edge/en-US-GuyNeural")

        mock_comm.assert_called_once_with("hello", "en-US-GuyNeural")


class TestOpenAIRoute:
    async def test_openai_voice_calls_openai_api(self):
        mock_response = MagicMock()
        mock_response.content = b"openai-audio"

        mock_client_instance = AsyncMock()
        mock_client_instance.audio.speech.create = AsyncMock(return_value=mock_response)

        with (
            patch("dispatch.tts._synthesize_openai") as mock_synth,
            patch("dispatch.tts.pygame.mixer.music.load"),
            patch("dispatch.tts.pygame.mixer.music.play"),
            patch("dispatch.tts.pygame.mixer.music.get_busy", return_value=False),
        ):
            mock_synth.return_value = BytesIO(b"openai-audio")
            await speak("hello", "openai/nova")

        mock_synth.assert_called_once_with("hello", "nova")

    async def test_openai_failure_falls_back_to_edge(self):
        mock_comm = _make_mock_communicate()

        with (
            patch("dispatch.tts._synthesize_openai", side_effect=RuntimeError("API down")),
            patch("dispatch.tts.edge_tts.Communicate", mock_comm),
            patch("dispatch.tts.pygame.mixer.music.load"),
            patch("dispatch.tts.pygame.mixer.music.play"),
            patch("dispatch.tts.pygame.mixer.music.get_busy", return_value=False),
        ):
            await speak("hello", "openai/nova", "en-US-AvaMultilingualNeural")

        mock_comm.assert_called_once_with("hello", "en-US-AvaMultilingualNeural")


class TestElevenLabsRoute:
    async def test_elevenlabs_voice_dispatches(self):
        with (
            patch("dispatch.tts._synthesize_elevenlabs") as mock_synth,
            patch("dispatch.tts.pygame.mixer.music.load"),
            patch("dispatch.tts.pygame.mixer.music.play"),
            patch("dispatch.tts.pygame.mixer.music.get_busy", return_value=False),
        ):
            mock_synth.return_value = BytesIO(b"elevenlabs-audio")
            await speak("hello", "elevenlabs/Rachel")

        mock_synth.assert_called_once_with("hello", "Rachel")


class TestGoogleRoute:
    async def test_google_voice_dispatches(self):
        with (
            patch("dispatch.tts._synthesize_google") as mock_synth,
            patch("dispatch.tts.pygame.mixer.music.load"),
            patch("dispatch.tts.pygame.mixer.music.play"),
            patch("dispatch.tts.pygame.mixer.music.get_busy", return_value=False),
        ):
            mock_synth.return_value = BytesIO(b"google-audio")
            await speak("hello", "google/en-US-Neural2-F")

        mock_synth.assert_called_once_with("hello", "en-US-Neural2-F")


# -- Fallback behavior ---------------------------------------------------------

class TestFallback:
    async def test_fallback_uses_specified_voice(self):
        """When primary provider fails, fallback_voice is used with Edge TTS."""
        mock_comm = _make_mock_communicate()

        with (
            patch("dispatch.tts._synthesize_google", side_effect=RuntimeError("quota exceeded")),
            patch("dispatch.tts.edge_tts.Communicate", mock_comm),
            patch("dispatch.tts.pygame.mixer.music.load"),
            patch("dispatch.tts.pygame.mixer.music.play"),
            patch("dispatch.tts.pygame.mixer.music.get_busy", return_value=False),
        ):
            await speak("hello", "google/en-US-Neural2-F", "en-US-AvaMultilingualNeural")

        mock_comm.assert_called_once_with("hello", "en-US-AvaMultilingualNeural")

    async def test_fallback_without_fallback_voice_uses_voice_name(self):
        """When no fallback_voice is set, Edge TTS uses the primary voice name."""
        mock_comm = _make_mock_communicate()

        with (
            patch("dispatch.tts._synthesize_openai", side_effect=RuntimeError("no key")),
            patch("dispatch.tts.edge_tts.Communicate", mock_comm),
            patch("dispatch.tts.pygame.mixer.music.load"),
            patch("dispatch.tts.pygame.mixer.music.play"),
            patch("dispatch.tts.pygame.mixer.music.get_busy", return_value=False),
        ):
            await speak("hello", "openai/nova")

        mock_comm.assert_called_once_with("hello", "nova")

    async def test_edge_primary_no_fallback_needed(self):
        """Edge TTS as primary should not trigger fallback logic."""
        mock_comm = _make_mock_communicate()

        with patch("dispatch.tts.edge_tts.Communicate", mock_comm):
            await speak("hello", "en-US-GuyNeural")

        mock_comm.assert_called_once_with("hello", "en-US-GuyNeural")


# -- Playback ------------------------------------------------------------------

class TestPlayback:
    async def test_speak_seeks_buffer_before_load(self):
        mock_comm = _make_mock_communicate(b"fake-audio")
        captured_buffer = None

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
        captured_buffer.seek(0)
        assert captured_buffer.read() == b"fake-audio"

    async def test_speak_loads_with_mp3_format(self):
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
        mock_comm = _make_mock_communicate()
        busy_returns = [True, True, False]

        with (
            patch("dispatch.tts.edge_tts.Communicate", mock_comm),
            patch("dispatch.tts.pygame.mixer.music.load"),
            patch("dispatch.tts.pygame.mixer.music.play"),
            patch("dispatch.tts.pygame.mixer.music.get_busy", side_effect=busy_returns),
        ):
            await speak("test", "en-US-GuyNeural")

    async def test_speak_empty_audio_data(self):
        mock_comm_instance = MagicMock()

        async def mock_stream():
            return
            yield

        mock_comm_instance.stream = mock_stream
        mock_comm_cls = MagicMock(return_value=mock_comm_instance)

        with (
            patch("dispatch.tts.edge_tts.Communicate", mock_comm_cls),
            patch("dispatch.tts.pygame.mixer.music.load") as mock_load,
        ):
            await speak("", "en-US-GuyNeural")

        mock_load.assert_not_called()
