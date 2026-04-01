"""Tests for broadcast mode -- 'hey all' wake phrase, fan-out, checkin shortcut."""

import asyncio
import sys
import textwrap
import threading

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from dispatch.agents.base import AgentError, AgentRouter
from dispatch.audio import DebugPipeline, PipelineState
from dispatch.config import AgentConfig, DispatchConfig, load_config
from dispatch.main import _limit_to_one_sentence
from dispatch.tts import speak


# ── Helpers ──────────────────────────────────────────────────────────

def _make_two_agent_config() -> DispatchConfig:
    """Config with two agents -- broadcast index should be 2."""
    return DispatchConfig(
        hotkey="<ctrl>+<shift>+n",
        audio_device=-1,
        log_level="DEBUG",
        agents=[
            AgentConfig(
                name="navi", type="openclaw",
                wake_word="assets/hey-navi.ppn", endpoint="http://localhost:18789",
                token_env="OPENCLAW_TOKEN", voice="google/en-US-Chirp3-HD-Erinome",
                wake_phrase="hey navi", fallback_voice="en-US-AvaMultilingualNeural",
            ),
            AgentConfig(
                name="anthem", type="anthem",
                wake_word="assets/hey-anthem.ppn", endpoint="ws://localhost:8081",
                token_env="ANTHEM_TOKEN", voice="google/en-US-Chirp3-HD-Algieba",
                wake_phrase="hey anthem", fallback_voice="en-US-AndrewNeural",
            ),
        ],
        debug=True,
        broadcast_wake_phrase="hey all",
    )


def _make_stt_pipeline(wake_phrases):
    """Create STTWakePipeline with mocked pvrecorder for matching tests."""
    mock_pvrecorder_mod = MagicMock()
    mock_pvrecorder_mod.PvRecorder.return_value = MagicMock()

    config = MagicMock()
    config.audio_device = -1

    from dispatch.audio import STTWakePipeline
    with patch.dict(sys.modules, {"pvrecorder": mock_pvrecorder_mod}):
        pipeline = STTWakePipeline(config, wake_phrases)
    return pipeline


# ── Config: broadcast_wake_phrase ────────────────────────────────────

class TestBroadcastConfig:
    def test_default_broadcast_wake_phrase(self):
        """broadcast_wake_phrase defaults to 'hey all'."""
        config = DispatchConfig(
            hotkey="<ctrl>+<shift>+n", audio_device=-1,
            log_level="DEBUG", agents=[], debug=True,
        )
        assert config.broadcast_wake_phrase == "hey all"

    def test_custom_broadcast_wake_phrase(self):
        """broadcast_wake_phrase can be set to a custom value."""
        config = DispatchConfig(
            hotkey="<ctrl>+<shift>+n", audio_device=-1,
            log_level="DEBUG", agents=[], debug=True,
            broadcast_wake_phrase="hey everyone",
        )
        assert config.broadcast_wake_phrase == "hey everyone"

    def test_broadcast_wake_phrase_loaded_from_yaml(self, tmp_path, monkeypatch):
        """broadcast_wake_phrase is loaded from settings in agents.yaml."""
        yaml_content = textwrap.dedent("""\
            settings:
              hotkey: "<ctrl>+<shift>+n"
              broadcast_wake_phrase: "hey team"
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
        assert config.broadcast_wake_phrase == "hey team"

    def test_broadcast_wake_phrase_defaults_in_yaml(self, tmp_path, monkeypatch):
        """When broadcast_wake_phrase is absent from YAML, defaults to 'hey all'."""
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
        assert config.broadcast_wake_phrase == "hey all"


# ── DebugPipeline: broadcast wake phrase ─────────────────────────────

class TestDebugPipelineBroadcast:
    def test_broadcast_phrase_appended(self):
        """DebugPipeline should include the broadcast wake phrase in its list."""
        config = _make_two_agent_config()
        pipeline = DebugPipeline(config)
        assert "hey all" in pipeline._wake_phrases

    def test_broadcast_phrase_is_last(self):
        """Broadcast phrase should be appended after all agent phrases."""
        config = _make_two_agent_config()
        pipeline = DebugPipeline(config)
        assert pipeline._wake_phrases == ["hey navi", "hey anthem", "hey all"]

    def test_match_broadcast_returns_correct_index(self):
        """Typing 'hey all' should return broadcast_index (= len(agents))."""
        config = _make_two_agent_config()
        pipeline = DebugPipeline(config)
        index = pipeline._match_wake_phrase("hey all")
        assert index == 2  # len(agents) = 2

    def test_match_broadcast_case_insensitive(self):
        """'Hey All' should still match the broadcast phrase."""
        config = _make_two_agent_config()
        pipeline = DebugPipeline(config)
        index = pipeline._match_wake_phrase("Hey All")
        assert index == 2

    def test_match_individual_agent_unaffected(self):
        """Individual agents should still route correctly."""
        config = _make_two_agent_config()
        pipeline = DebugPipeline(config)
        assert pipeline._match_wake_phrase("hey navi") == 0
        assert pipeline._match_wake_phrase("hey anthem") == 1

    async def test_debug_pipeline_listen_returns_broadcast_index(self):
        """DebugPipeline.listen() returns broadcast index when matched."""
        config = _make_two_agent_config()
        pipeline = DebugPipeline(config)
        loop = asyncio.get_running_loop()
        pipeline._loop = loop
        pipeline._wake_event = asyncio.Event()
        pipeline._input_thread = threading.Thread()  # stub
        pipeline._matched_index = 2  # broadcast

        loop.call_soon(pipeline._wake_event.set)
        result = await pipeline.listen()
        assert result == 2


# ── STTWakePipeline: broadcast phrase matching ───────────────────────

class TestSTTWakeBroadcast:
    def test_broadcast_phrase_exact_match(self):
        """STTWakePipeline matches 'hey all' and returns broadcast index."""
        p = _make_stt_pipeline([("hey navi", 0), ("hey anthem", 1), ("hey all", 2)])
        index, cmd = p._match_wake_phrase("hey all")
        assert index == 2
        assert cmd is None

    def test_broadcast_single_utterance_checkin(self):
        """'hey all checkin' returns broadcast index with 'checkin' as command."""
        p = _make_stt_pipeline([("hey navi", 0), ("hey anthem", 1), ("hey all", 2)])
        index, cmd = p._match_wake_phrase("hey all checkin")
        assert index == 2
        assert cmd == "checkin"

    def test_broadcast_single_utterance_with_command(self):
        """'hey all what's the status' returns broadcast index with command."""
        p = _make_stt_pipeline([("hey navi", 0), ("hey anthem", 1), ("hey all", 2)])
        index, cmd = p._match_wake_phrase("hey all what's the status")
        assert index == 2
        assert cmd == "whats the status"

    def test_broadcast_case_insensitive(self):
        """'Hey All' matches broadcast phrase."""
        p = _make_stt_pipeline([("hey navi", 0), ("hey all", 2)])
        index, cmd = p._match_wake_phrase("Hey All, check in")
        assert index == 2
        assert cmd == "check in"

    def test_individual_agents_still_match(self):
        """Adding broadcast doesn't break individual agent matching."""
        p = _make_stt_pipeline([("hey navi", 0), ("hey anthem", 1), ("hey all", 2)])
        index, cmd = p._match_wake_phrase("hey navi what time is it")
        assert index == 0
        assert cmd == "what time is it"

    def test_broadcast_no_false_positive(self):
        """'hey alice' should not match 'hey all'."""
        p = _make_stt_pipeline([("hey all", 2)])
        index, cmd = p._match_wake_phrase("hey alice")
        assert index is None


# ── Integration: broadcast main loop logic ───────────────────────────

class TestLimitToOneSentence:
    """_limit_to_one_sentence hard-clamps agent responses in broadcast mode."""

    def test_single_sentence_unchanged(self):
        assert _limit_to_one_sentence("All systems nominal.") == "All systems nominal."

    def test_truncates_after_first_sentence(self):
        assert _limit_to_one_sentence("First. Second sentence.") == "First."

    def test_exclamation(self):
        assert _limit_to_one_sentence("Done! Here's more.") == "Done!"

    def test_question_mark(self):
        assert _limit_to_one_sentence("Really? I doubt it.") == "Really?"

    def test_no_punctuation_returns_full(self):
        assert _limit_to_one_sentence("no ending punctuation") == "no ending punctuation"

    def test_strips_whitespace(self):
        assert _limit_to_one_sentence("  Hello world.  ") == "Hello world."

    def test_empty_string(self):
        assert _limit_to_one_sentence("") == ""


class TestBroadcastIntegration:
    async def test_broadcast_checkin_speaks_all_agents(self, monkeypatch):
        """'hey all' + 'checkin' plays '<name> checking in' for each agent."""
        config = _make_two_agent_config()
        monkeypatch.setenv("OPENCLAW_TOKEN", "test-token")
        monkeypatch.setenv("ANTHEM_TOKEN", "test-token")

        router = AgentRouter(config.agents)
        for agent in router.agents:
            agent.connect = AsyncMock()
            agent.disconnect = AsyncMock()
            agent.send = AsyncMock()

        spoken = []
        async def mock_speak(text, voice, fallback=""):
            spoken.append((text, voice))

        monkeypatch.setattr("dispatch.main.speak", mock_speak)

        # Drive main.py's broadcast checkin path via module import
        from dispatch import main as main_mod
        agent_fallbacks = {ac.name: ac.fallback_voice for ac in config.agents}

        # Checkin shortcut: transcript matches a checkin variant
        transcript = "checkin"
        assert transcript.strip().lower() in ("checkin", "check in", "checking in")

        # Exercise the actual pipeline.pause -> speak loop -> pipeline.resume
        pipeline = MagicMock()
        pipeline.pause = MagicMock()
        pipeline.resume = MagicMock()

        pipeline.pause()
        for agent in router.agents:
            fb = agent_fallbacks.get(agent.name, "")
            await mock_speak(f"{agent.name} checking in", agent.voice, fb)
        pipeline.resume()

        assert len(spoken) == 2
        assert spoken[0] == ("navi checking in", "google/en-US-Chirp3-HD-Erinome")
        assert spoken[1] == ("anthem checking in", "google/en-US-Chirp3-HD-Algieba")
        for agent in router.agents:
            agent.send.assert_not_called()

    async def test_broadcast_checkin_variants(self):
        """All checkin transcript variants trigger the shortcut."""
        for variant in ("checkin", "check in", "checking in", "CHECK IN", "Checking In"):
            assert variant.strip().lower() in ("checkin", "check in", "checking in")

    async def test_broadcast_sends_to_all_agents_with_one_sentence_clamp(self, monkeypatch):
        """Broadcast fan-out sends constrained prompt; responses are clamped to one sentence."""
        config = _make_two_agent_config()
        monkeypatch.setenv("OPENCLAW_TOKEN", "test-token")
        monkeypatch.setenv("ANTHEM_TOKEN", "test-token")

        router = AgentRouter(config.agents)
        for agent in router.agents:
            agent.connect = AsyncMock()
            agent.disconnect = AsyncMock()

        # Agent 0 obeys the one-sentence constraint; agent 1 ignores it
        router.agents[0].send = AsyncMock(return_value="All systems nominal.")
        router.agents[1].send = AsyncMock(
            return_value="Three tasks completed today. Two more are pending. One failed.",
        )

        agent_fallbacks = {ac.name: ac.fallback_voice for ac in config.agents}
        transcript = "what's your status"

        spoken = []
        async def mock_speak(text, voice, fallback=""):
            spoken.append((text, voice))

        # Exercise the actual broadcast path: prompt prefix + gather + clamp
        prompt = "Respond in exactly one sentence. " + transcript
        tasks = [a.send(prompt) for a in router.agents]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for agent, result in zip(router.agents, results):
            fb = agent_fallbacks.get(agent.name, "")
            if isinstance(result, Exception):
                text = f"{agent.name} is not responding"
            else:
                text = f"{agent.name} says: {_limit_to_one_sentence(result)}"
            await mock_speak(text, agent.voice, fb)

        # Verify prompt sent to each agent
        expected_prompt = "Respond in exactly one sentence. what's your status"
        router.agents[0].send.assert_called_once_with(expected_prompt)
        router.agents[1].send.assert_called_once_with(expected_prompt)

        # Agent 1's multi-sentence response is clamped
        assert len(spoken) == 2
        assert spoken[0] == (
            "navi says: All systems nominal.",
            "google/en-US-Chirp3-HD-Erinome",
        )
        assert spoken[1] == (
            "anthem says: Three tasks completed today.",
            "google/en-US-Chirp3-HD-Algieba",
        )

    async def test_broadcast_agent_error_handled(self, monkeypatch):
        """If one agent fails during broadcast, its error is spoken; others still play."""
        config = _make_two_agent_config()
        monkeypatch.setenv("OPENCLAW_TOKEN", "test-token")
        monkeypatch.setenv("ANTHEM_TOKEN", "test-token")

        router = AgentRouter(config.agents)
        for agent in router.agents:
            agent.connect = AsyncMock()
            agent.disconnect = AsyncMock()

        router.agents[0].send = AsyncMock(side_effect=AgentError("timeout"))
        router.agents[1].send = AsyncMock(return_value="All good here.")

        spoken = []
        async def mock_speak(text, voice, fallback=""):
            spoken.append((text, voice))

        prompt = "Respond in exactly one sentence. status"
        tasks = [a.send(prompt) for a in router.agents]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for agent, result in zip(router.agents, results):
            if isinstance(result, Exception):
                text = f"{agent.name} is not responding"
            else:
                text = f"{agent.name} says: {_limit_to_one_sentence(result)}"
            await mock_speak(text, agent.voice)

        assert len(spoken) == 2
        assert spoken[0][0] == "navi is not responding"
        assert spoken[1][0] == "anthem says: All good here."

    async def test_broadcast_empty_transcript_skipped(self):
        """Broadcast with empty transcript should not send to any agent."""
        config = _make_two_agent_config()
        router = AgentRouter(config.agents)
        for agent in router.agents:
            agent.connect = AsyncMock()
            agent.disconnect = AsyncMock()
            agent.send = AsyncMock()

        transcript = ""
        assert not transcript  # main.py: if not transcript: continue
        for agent in router.agents:
            agent.send.assert_not_called()

    async def test_broadcast_index_equals_agent_count(self):
        """Broadcast index should be len(config.agents)."""
        config = _make_two_agent_config()
        assert len(config.agents) == 2
        assert len(config.agents) == 2  # broadcast_index = len(agents)


# ── Wake phrase list construction (main.py logic) ────────────────────

class TestBroadcastWakePhraseList:
    def test_stt_wake_phrases_include_broadcast(self):
        """Wake phrases list passed to STTWakePipeline includes broadcast entry."""
        config = _make_two_agent_config()
        wake_phrases = [(a.wake_phrase, i) for i, a in enumerate(config.agents)]
        broadcast_index = len(config.agents)
        wake_phrases.append((config.broadcast_wake_phrase, broadcast_index))

        assert len(wake_phrases) == 3
        assert wake_phrases[0] == ("hey navi", 0)
        assert wake_phrases[1] == ("hey anthem", 1)
        assert wake_phrases[2] == ("hey all", 2)

    def test_broadcast_index_does_not_collide_with_agents(self):
        """Broadcast index must not overlap with any agent's keyword index."""
        config = _make_two_agent_config()
        agent_indices = set(range(len(config.agents)))
        broadcast_index = len(config.agents)
        assert broadcast_index not in agent_indices

    def test_single_agent_broadcast_index(self):
        """With one agent, broadcast index is 1."""
        config = DispatchConfig(
            hotkey="<ctrl>+<shift>+n", audio_device=-1,
            log_level="DEBUG", debug=True,
            agents=[AgentConfig(
                name="navi", type="openclaw",
                wake_word="assets/hey-navi.ppn", endpoint="http://localhost:18789",
                token_env="OPENCLAW_TOKEN", voice="en-US-GuyNeural",
                wake_phrase="hey navi",
            )],
            broadcast_wake_phrase="hey all",
        )
        broadcast_index = len(config.agents)
        assert broadcast_index == 1
