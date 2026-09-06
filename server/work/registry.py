"""Configured AgentProvider registry with a test injection seam."""

from __future__ import annotations

from functools import lru_cache

from server.work.anthropic_provider import AnthropicManagedAgentProvider
from server.work.config import load_work_config
from server.work.fake_provider import FakeAgentProvider
from server.work.provider import AgentProvider


@lru_cache(maxsize=1)
def get_agent_provider() -> AgentProvider:
    config = load_work_config()
    if config.provider == "fake":
        return FakeAgentProvider()
    return AnthropicManagedAgentProvider(config)
