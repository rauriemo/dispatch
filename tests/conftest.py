"""Shared fixtures for Dispatch test suite."""

import pytest
from unittest.mock import patch, MagicMock

from dispatch.config import AgentConfig, DispatchConfig


@pytest.fixture
def sample_agent_config() -> AgentConfig:
    return AgentConfig(
        name="navi",
        type="openclaw",
        wake_word="assets/hey-navi.ppn",
        endpoint="http://localhost:18789",
        token_env="OPENCLAW_TOKEN",
        voice="en-US-GuyNeural",
    )


@pytest.fixture
def sample_config(sample_agent_config) -> DispatchConfig:
    return DispatchConfig(
        hotkey="<ctrl>+<shift>+n",
        audio_device=-1,
        log_level="DEBUG",
        agents=[sample_agent_config],
        debug=True,
    )


@pytest.fixture(autouse=True)
def mock_pygame():
    """Prevent pygame from actually initializing audio hardware in tests."""
    mock_sound = MagicMock()
    with (
        patch("pygame.mixer.init"),
        patch("pygame.mixer.quit"),
        patch("pygame.mixer.music.load"),
        patch("pygame.mixer.music.play"),
        patch("pygame.mixer.music.get_busy", return_value=False),
        patch("pygame.mixer.Sound", return_value=mock_sound),
    ):
        yield
