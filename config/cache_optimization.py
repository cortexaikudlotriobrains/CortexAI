"""Feature flags and versions for cache-aware usage optimization.

Flags are intentionally evaluated at call time so tests, worker reloads, and
deployment overrides can change one optimization without rebuilding global
service objects.
"""

from __future__ import annotations

import os


CACHE_AWARE_CREDIT_POLICY_VERSION = "cache-aware-v1"
CORTEX_PROMPT_STRUCTURE_VERSION = "cortex-prompt-v2"
CONTEXT_SUMMARY_POLICY_VERSION = "context-summary-v1"
OPTIMIZER_PROMPT_VERSION = "optimizer-prompt-v1"
CORTEX_ANALYSIS_POLICY_VERSION = "cortex-analysis-v2-editorial"


def env_flag(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def cache_aware_credit_calculation_enabled() -> bool:
    return env_flag("CACHE_AWARE_CREDIT_CALCULATION_ENABLED", default=True)


def cache_aware_credit_settlement_enabled() -> bool:
    # Shadow mode is the safe initial rollout state.
    return env_flag("CACHE_AWARE_CREDIT_SETTLEMENT_ENABLED", default=False)


def cache_friendly_prompt_ordering_enabled() -> bool:
    return env_flag("CACHE_FRIENDLY_PROMPT_ORDERING_ENABLED", default=False)


def openai_prompt_cache_enabled() -> bool:
    return env_flag("OPENAI_PROMPT_CACHE_ENABLED", default=False)


def openai_extended_prompt_cache_enabled() -> bool:
    return env_flag("OPENAI_EXTENDED_PROMPT_CACHE_ENABLED", default=False)


def claude_prompt_cache_enabled() -> bool:
    return env_flag("CLAUDE_PROMPT_CACHE_ENABLED", default=False)


def grok_prompt_cache_enabled() -> bool:
    return env_flag("GROK_PROMPT_CACHE_ENABLED", default=False)


def persistent_research_reuse_enabled() -> bool:
    return env_flag("PERSISTENT_RESEARCH_REUSE_ENABLED", default=False)


def prompt_optimization_reuse_enabled() -> bool:
    return env_flag("PROMPT_OPTIMIZATION_REUSE_ENABLED", default=False)


def cortex_analysis_reuse_enabled() -> bool:
    return env_flag("CORTEX_ANALYSIS_REUSE_ENABLED", default=False)


def credit_aware_generation_budget_enabled() -> bool:
    return env_flag("CREDIT_AWARE_GENERATION_BUDGET_ENABLED", default=False)


def context_compaction_enabled() -> bool:
    return env_flag("CONTEXT_COMPACTION_ENABLED", default=False)
