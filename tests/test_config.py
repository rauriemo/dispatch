"""Tests for dispatch.config -- YAML parsing, validation, env vars."""

import textwrap

import pytest

from dispatch.config import load_config


class TestLoadConfig:
    def test_load_valid_config(self, tmp_path, monkeypatch):
        """Parse well-formed agents.yaml, verify all fields populated."""
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

        assert config.hotkey == "<ctrl>+<shift>+n"
        assert config.audio_device == -1
        assert config.log_level == "DEBUG"
        assert config.debug is True
        assert len(config.agents) == 1

        agent = config.agents[0]
        assert agent.name == "navi"
        assert agent.type == "openclaw"
        assert agent.wake_word == "assets/hey-navi.ppn"
        assert agent.endpoint == "http://localhost:18789"
        assert agent.token_env == "OPENCLAW_TOKEN"
        assert agent.voice == "en-US-GuyNeural"

    def test_missing_required_field_raises(self, tmp_path, monkeypatch):
        """agents.yaml with missing endpoint field should raise KeyError."""
        yaml_content = textwrap.dedent("""\
            settings:
              hotkey: "<ctrl>+<shift>+n"
            agents:
              navi:
                type: openclaw
                wake_word: assets/hey-navi.ppn
                token_env: OPENCLAW_TOKEN
                voice: en-US-GuyNeural
        """)
        yaml_path = tmp_path / "agents.yaml"
        yaml_path.write_text(yaml_content)

        monkeypatch.setattr("dispatch.config.PROJECT_ROOT", tmp_path)

        with pytest.raises(KeyError):
            load_config(debug=True)

    def test_env_var_validation_debug_mode(self, tmp_path, monkeypatch):
        """In debug mode, missing PICOVOICE/GCP keys should NOT raise."""
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
        monkeypatch.delenv("PICOVOICE_ACCESS_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

        # Should not raise
        config = load_config(debug=True)
        assert config.debug is True

    def test_env_var_validation_live_mode(self, tmp_path, monkeypatch):
        """Outside debug mode, missing PICOVOICE key logs warning (doesn't raise)."""
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
        monkeypatch.delenv("PICOVOICE_ACCESS_KEY", raising=False)
        monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
        monkeypatch.setenv("OPENCLAW_TOKEN", "test-token")

        # _validate logs warnings but doesn't raise for missing keys
        config = load_config(debug=False)
        assert config.debug is False

    def test_hotkey_format_preserved(self, tmp_path, monkeypatch):
        """Hotkey field must preserve angle brackets exactly."""
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
        assert config.hotkey == "<ctrl>+<shift>+n"
        assert "<" in config.hotkey and ">" in config.hotkey

    def test_no_agents_raises(self, tmp_path, monkeypatch):
        """Empty agents section should raise ValueError."""
        yaml_content = textwrap.dedent("""\
            settings:
              hotkey: "<ctrl>+<shift>+n"
            agents: {}
        """)
        yaml_path = tmp_path / "agents.yaml"
        yaml_path.write_text(yaml_content)

        monkeypatch.setattr("dispatch.config.PROJECT_ROOT", tmp_path)

        with pytest.raises(ValueError, match="No agents defined"):
            load_config(debug=True)
