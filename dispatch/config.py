"""Typed configuration loaded from agents.yaml + .env."""

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _derive_wake_phrase(wake_word_path: str) -> str:
    """Derive a text wake phrase from a .ppn filename.

    'assets/hey-navi.ppn' -> 'hey navi'
    """
    stem = Path(wake_word_path).stem
    # Strip platform suffixes like '_en_windows'
    for suffix in ("_en_windows", "_en_mac", "_en_linux", "_en_raspberry-pi"):
        stem = stem.removesuffix(suffix)
    return stem.replace("-", " ").replace("_", " ").lower()


@dataclass
class AgentConfig:
    name: str
    type: str
    wake_word: str
    endpoint: str
    token_env: str
    voice: str
    wake_phrase: str = ""
    fallback_voice: str = ""


@dataclass
class DispatchConfig:
    hotkey: str
    audio_device: int
    log_level: str
    agents: list[AgentConfig]
    debug: bool
    webhook_port: int = 0
    broadcast_wake_phrase: str = "hey all"


def load_config(debug: bool = False) -> DispatchConfig:
    """Load .env, parse agents.yaml, validate, and return typed config."""
    load_dotenv(PROJECT_ROOT / ".env")

    yaml_path = PROJECT_ROOT / "agents.yaml"
    if not yaml_path.exists():
        raise FileNotFoundError(f"agents.yaml not found at {yaml_path}")

    with open(yaml_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    settings = raw.get("settings", {})
    hotkey = settings.get("hotkey", "<ctrl>+<shift>+n")
    audio_device = settings.get("audio_device", -1)
    log_level = settings.get("log_level", "INFO")

    agents_raw = raw.get("agents", {})
    agents: list[AgentConfig] = []
    for name, cfg in agents_raw.items():
        wake_word = cfg["wake_word"]
        wake_phrase = cfg.get("wake_phrase", "") or _derive_wake_phrase(wake_word)
        agents.append(AgentConfig(
            name=name,
            type=cfg["type"],
            wake_word=wake_word,
            endpoint=cfg["endpoint"],
            token_env=cfg["token_env"],
            voice=cfg["voice"],
            wake_phrase=wake_phrase,
            fallback_voice=cfg.get("fallback_voice", ""),
        ))

    webhook_port = settings.get("webhook_port", 0)
    broadcast_wake_phrase = settings.get("broadcast_wake_phrase", "hey all")

    config = DispatchConfig(
        hotkey=hotkey,
        audio_device=audio_device,
        log_level=log_level,
        agents=agents,
        debug=debug,
        webhook_port=webhook_port,
        broadcast_wake_phrase=broadcast_wake_phrase,
    )

    _validate(config)

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    return config


def _validate(config: DispatchConfig) -> None:
    """Validate config, raising on critical issues."""
    if not config.agents:
        raise ValueError("No agents defined in agents.yaml")

    for agent in config.agents:
        # Token env var must be set
        token = os.environ.get(agent.token_env)
        if not token:
            logger.warning(
                "Env var %s not set for agent '%s' -- agent may fail at runtime",
                agent.token_env, agent.name,
            )

        # Wake word file must exist (unless debug mode)
        if not config.debug:
            ppn_path = PROJECT_ROOT / agent.wake_word
            if not ppn_path.exists():
                logger.warning(
                    "Wake word file %s not found for agent '%s' -- "
                    "falling back to debug pipeline",
                    ppn_path, agent.name,
                )

    if not config.debug:
        if not os.environ.get("PICOVOICE_ACCESS_KEY"):
            if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
                logger.warning(
                    "PICOVOICE_ACCESS_KEY not set -- will use STT-based wake word detection"
                )
            else:
                logger.warning(
                    "PICOVOICE_ACCESS_KEY not set -- will fall back to debug pipeline"
                )
        if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            logger.warning(
                "GOOGLE_APPLICATION_CREDENTIALS not set -- will fall back to debug STT"
            )
