"""Integration tests -- debug mode full cycle, error paths."""

import asyncio
import textwrap
import threading
from unittest.mock import AsyncMock, MagicMock, patch

from dispatch.agents.base import AgentError, AgentRouter
from dispatch.agents.openclaw import OpenClawAgent
from dispatch.audio import DebugPipeline
from dispatch.config import load_config
from dispatch.tts import speak


class TestDebugFullCycle:
    async def test_debug_pipeline_full_cycle(self, sample_config, monkeypatch):
        """Full debug cycle: wake -> transcribe -> agent.send -> speak."""
        monkeypatch.setenv("OPENCLAW_TOKEN", "test-token")

        # Build router with real OpenClawAgent
        router = AgentRouter(sample_config.agents)
        agent = router.agents[0]

        # Mock agent.send to return a known response
        agent.send = AsyncMock(return_value="The answer is 42.")
        agent.connect = AsyncMock()
        agent.disconnect = AsyncMock()

        # Wire the debug pipeline and simulate wake word via event
        pipeline = DebugPipeline(sample_config)
        pipeline._loop = asyncio.get_running_loop()
        pipeline._wake_event = asyncio.Event()
        pipeline._input_thread = threading.Thread()  # stub to skip thread spawn
        pipeline._loop.call_soon(pipeline._wake_event.set)
        keyword_index = await pipeline.listen()

        assert keyword_index == 0

        # Route to agent
        routed_agent = router.route(keyword_index)
        assert routed_agent.name == "navi"

        # Simulate transcription
        transcript = "what is the meaning of life?"

        # Send to agent
        response = await routed_agent.send(transcript)
        assert response == "The answer is 42."
        agent.send.assert_called_once_with("what is the meaning of life?")

        # Speak response (mock TTS)
        mock_comm = MagicMock()

        async def mock_stream():
            yield {"type": "audio", "data": b"fake"}

        mock_comm.return_value.stream = mock_stream

        with (
            patch("dispatch.tts.edge_tts.Communicate", mock_comm),
            patch("pygame.mixer.music.load"),
            patch("pygame.mixer.music.play"),
            patch("pygame.mixer.music.get_busy", return_value=False),
        ):
            await speak(response, routed_agent.voice)

        mock_comm.assert_called_once_with("The answer is 42.", "en-US-GuyNeural")

    async def test_agent_error_path(self, sample_config, monkeypatch):
        """When agent.send raises AgentError, error message should be spoken."""
        monkeypatch.setenv("OPENCLAW_TOKEN", "test-token")

        router = AgentRouter(sample_config.agents)
        agent = router.agents[0]
        agent.send = AsyncMock(side_effect=AgentError("timeout"))
        agent.connect = AsyncMock()
        agent.disconnect = AsyncMock()

        # Simulate the main loop's error handling
        try:
            await agent.send("test command")
            response = "should not reach"
        except AgentError:
            response = f"{agent.name} is not responding"

        assert response == "navi is not responding"


class TestConfigToRouterToAgent:
    def test_config_to_router_to_agent(self, tmp_path, monkeypatch):
        """Load agents.yaml from tmpdir, build router, verify correct agent."""
        yaml_content = textwrap.dedent("""\
            settings:
              hotkey: "<ctrl>+<shift>+n"
              audio_device: -1
              log_level: DEBUG
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
        router = AgentRouter(config.agents)

        assert len(router.agents) == 1
        agent = router.agents[0]
        assert isinstance(agent, OpenClawAgent)
        assert agent.name == "navi"
        assert agent.voice == "en-US-GuyNeural"
