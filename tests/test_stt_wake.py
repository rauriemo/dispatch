"""Tests for STTWakePipeline -- wake phrase matching, pipeline behavior, config derivation."""

import asyncio
import queue
import sys
import threading

import pytest
from unittest.mock import patch, MagicMock

from dispatch.audio import PipelineState, STTWakePipeline
from dispatch.config import _derive_wake_phrase


# -- Wake phrase derivation from .ppn filename ---------------------------------

class TestDeriveWakePhrase:
    def test_simple_filename(self):
        assert _derive_wake_phrase("assets/hey-navi.ppn") == "hey navi"

    def test_platform_suffix_stripped(self):
        assert _derive_wake_phrase("assets/hey-navi_en_windows.ppn") == "hey navi"

    def test_mac_suffix_stripped(self):
        assert _derive_wake_phrase("assets/hey-navi_en_mac.ppn") == "hey navi"

    def test_underscores_become_spaces(self):
        assert _derive_wake_phrase("assets/ok_google.ppn") == "ok google"

    def test_already_lowercase(self):
        assert _derive_wake_phrase("HEY-NAVI.ppn") == "hey navi"

    def test_nested_path(self):
        assert _derive_wake_phrase("some/deep/path/hey-jarvis.ppn") == "hey jarvis"


# -- Wake phrase matching logic ------------------------------------------------

def _make_pipeline_for_matching(wake_phrases):
    """Create a minimal STTWakePipeline with mocked pvrecorder for matching tests."""
    mock_pvrecorder_mod = MagicMock()
    mock_recorder = MagicMock()
    mock_pvrecorder_mod.PvRecorder.return_value = mock_recorder

    config = MagicMock()
    config.audio_device = -1

    with patch.dict(sys.modules, {"pvrecorder": mock_pvrecorder_mod}):
        pipeline = STTWakePipeline(config, wake_phrases)
    return pipeline


class TestWakePhraseMatching:
    def test_exact_match_no_command(self):
        p = _make_pipeline_for_matching([("hey navi", 0)])
        index, cmd = p._match_wake_phrase("hey navi")
        assert index == 0
        assert cmd is None

    def test_single_utterance_with_command(self):
        p = _make_pipeline_for_matching([("hey navi", 0)])
        index, cmd = p._match_wake_phrase("hey navi what's the weather")
        assert index == 0
        assert cmd == "whats the weather"

    def test_case_insensitive(self):
        p = _make_pipeline_for_matching([("hey navi", 0)])
        index, cmd = p._match_wake_phrase("Hey Navi")
        assert index == 0
        assert cmd is None

    def test_punctuation_after_phrase(self):
        p = _make_pipeline_for_matching([("hey navi", 0)])
        index, cmd = p._match_wake_phrase("Hey Navi, check the deploy")
        assert index == 0
        assert cmd == "check the deploy"

    def test_no_match(self):
        p = _make_pipeline_for_matching([("hey navi", 0)])
        index, cmd = p._match_wake_phrase("hello world")
        assert index is None
        assert cmd is None

    def test_multiple_agents_first_match(self):
        p = _make_pipeline_for_matching([("hey navi", 0), ("hey jarvis", 1)])
        index, cmd = p._match_wake_phrase("hey jarvis open the pod bay doors")
        assert index == 1
        assert cmd == "open the pod bay doors"

    def test_multiple_agents_navi(self):
        p = _make_pipeline_for_matching([("hey navi", 0), ("hey jarvis", 1)])
        index, cmd = p._match_wake_phrase("hey navi")
        assert index == 0

    def test_empty_transcript(self):
        p = _make_pipeline_for_matching([("hey navi", 0)])
        index, cmd = p._match_wake_phrase("")
        assert index is None
        assert cmd is None

    def test_phrase_with_extra_whitespace(self):
        p = _make_pipeline_for_matching([("hey navi", 0)])
        index, cmd = p._match_wake_phrase("  hey navi  tell me a joke  ")
        assert index == 0
        assert cmd == "tell me a joke"

    def test_smart_apostrophe_navi(self):
        """Google STT transcribes 'navi' as 'na\u2019vi' -- must still match."""
        p = _make_pipeline_for_matching([("hey navi", 0)])
        index, cmd = p._match_wake_phrase("Hey na\u2019vi, can you hear me?")
        assert index == 0
        assert cmd == "can you hear me"

    def test_smart_apostrophe_with_command(self):
        p = _make_pipeline_for_matching([("hey navi", 0)])
        index, cmd = p._match_wake_phrase("Hey na\u2019vi. What\u2019s the time?")
        assert index == 0
        assert cmd == "whats the time"


# -- STTWakePipeline behavior --------------------------------------------------

class TestSTTWakePipeline:
    def _make_pipeline(self):
        return _make_pipeline_for_matching([("hey navi", 0)])

    def test_has_frame_queue(self):
        p = self._make_pipeline()
        assert hasattr(p, "frame_queue")
        assert isinstance(p.frame_queue, queue.Queue)

    def test_has_pending_command(self):
        p = self._make_pipeline()
        assert hasattr(p, "pending_command")
        assert p.pending_command is None

    def test_initial_state_is_listening(self):
        p = self._make_pipeline()
        assert p._state == PipelineState.LISTENING

    def test_pause_sets_paused(self):
        p = self._make_pipeline()
        p.pause()
        assert p._state == PipelineState.PAUSED

    def test_resume_sets_listening(self):
        p = self._make_pipeline()
        p.pause()
        p.resume()
        assert p._state == PipelineState.LISTENING

    def test_set_state(self):
        p = self._make_pipeline()
        p.set_state(PipelineState.RECORDING)
        assert p._state == PipelineState.RECORDING

    async def test_listen_timeout_returns_none(self):
        p = self._make_pipeline()
        p._loop = asyncio.get_running_loop()
        p._wake_event = asyncio.Event()
        result = await p.listen(timeout=0.05)
        assert result is None

    async def test_listen_returns_keyword_index(self):
        p = self._make_pipeline()
        p._loop = asyncio.get_running_loop()
        p._wake_event = asyncio.Event()
        p._keyword_index = 0

        loop = asyncio.get_running_loop()
        loop.call_soon(p._wake_event.set)
        result = await p.listen(timeout=1.0)
        assert result == 0

    def test_context_manager_stop(self):
        """Context manager __exit__ calls stop without error."""
        p = self._make_pipeline()
        p._thread = None
        p.__exit__(None, None, None)

    def test_stop_without_thread(self):
        p = self._make_pipeline()
        p.stop()


# -- Config integration: wake_phrase field -------------------------------------

class TestConfigWakePhrase:
    def test_wake_phrase_auto_derived(self, tmp_path, monkeypatch):
        """wake_phrase should be auto-derived from wake_word filename."""
        import textwrap
        from dispatch.config import load_config

        yaml_content = textwrap.dedent("""\
            settings:
              hotkey: "<ctrl>+<shift>+n"
            agents:
              navi:
                type: openclaw
                wake_word: assets/hey-navi.ppn
                endpoint: http://localhost:18789
                token_env: OPENCLAW_TOKEN
                voice: en-US-GuyNeural
        """)
        yaml_path = tmp_path / "agents.yaml"
        yaml_path.write_text(yaml_content)

        monkeypatch.setattr("dispatch.config.PROJECT_ROOT", tmp_path)
        monkeypatch.setenv("OPENCLAW_TOKEN", "test-token")

        config = load_config(debug=True)
        assert config.agents[0].wake_phrase == "hey navi"

    def test_wake_phrase_explicit_override(self, tmp_path, monkeypatch):
        """Explicit wake_phrase in YAML overrides auto-derivation."""
        import textwrap
        from dispatch.config import load_config

        yaml_content = textwrap.dedent("""\
            settings:
              hotkey: "<ctrl>+<shift>+n"
            agents:
              navi:
                type: openclaw
                wake_word: assets/hey-navi.ppn
                wake_phrase: "yo navi"
                endpoint: http://localhost:18789
                token_env: OPENCLAW_TOKEN
                voice: en-US-GuyNeural
        """)
        yaml_path = tmp_path / "agents.yaml"
        yaml_path.write_text(yaml_content)

        monkeypatch.setattr("dispatch.config.PROJECT_ROOT", tmp_path)
        monkeypatch.setenv("OPENCLAW_TOKEN", "test-token")

        config = load_config(debug=True)
        assert config.agents[0].wake_phrase == "yo navi"
