"""Base agent interface, error type, and routing."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from dispatch.notifications import NotificationQueue

logger = logging.getLogger(__name__)


class AgentError(Exception):
    """Raised when an agent fails to process a request."""


class BaseAgent(ABC):
    def __init__(self, name: str, voice: str) -> None:
        self.name = name
        self.voice = voice

    @abstractmethod
    async def send(self, text: str) -> str: ...

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    async def subscribe(self, queue: NotificationQueue) -> None:
        """Override for push notification support."""


class AgentRouter:
    """Maps wake-word keyword indices to agent instances.

    Async context manager: __aenter__ connects all agents, __aexit__ disconnects.
    """

    REGISTRY: dict[str, type[BaseAgent]] = {}

    @classmethod
    def register(cls, type_name: str, agent_cls: type[BaseAgent]) -> None:
        cls.REGISTRY[type_name] = agent_cls

    def __init__(self, agent_configs: list) -> None:
        from dispatch.config import PROJECT_ROOT, AgentConfig

        self.agents: list[BaseAgent] = []
        self._ppn_paths: list[str] = []

        for cfg in agent_configs:
            cfg: AgentConfig
            agent_cls = self.REGISTRY.get(cfg.type)
            if agent_cls is None:
                logger.error("Unknown agent type '%s' for agent '%s'", cfg.type, cfg.name)
                continue

            agent = agent_cls(
                name=cfg.name,
                voice=cfg.voice,
                endpoint=cfg.endpoint,
                token_env=cfg.token_env,
            )
            self.agents.append(agent)
            self._ppn_paths.append(str(PROJECT_ROOT / cfg.wake_word))

    @property
    def ppn_paths(self) -> list[str]:
        return self._ppn_paths

    def route(self, keyword_index: int) -> BaseAgent:
        return self.agents[keyword_index]

    async def __aenter__(self) -> AgentRouter:
        for agent in self.agents:
            try:
                await agent.connect()
                logger.info("Agent '%s' connected", agent.name)
            except Exception:
                logger.warning(
                    "Agent '%s' failed to connect -- degraded",
                    agent.name,
                    exc_info=True,
                )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        for agent in self.agents:
            try:
                await agent.disconnect()
            except Exception:
                logger.warning("Error disconnecting agent '%s'", agent.name, exc_info=True)
