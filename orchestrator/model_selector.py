from __future__ import annotations

from orchestrator.routing_types import ModelCandidate, PromptFeatures, RoutingConstraints, SelectionResult


class ReliabilityStore:
    def get(self, provider: str, model: str) -> float:
        return 1.0


class ModelSelector:
    def __init__(self, reliability_store: ReliabilityStore | None = None, token_buffer: int = 200):
        self._reliability_store = reliability_store or ReliabilityStore()
        self._token_buffer = token_buffer

    def select(
        self,
        features: PromptFeatures,
        candidates: list[ModelCandidate],
        constraints: RoutingConstraints | None = None,
    ) -> SelectionResult:
        required_tokens = (
            features.token_estimate + features.context_token_estimate + self._token_buffer
        )

        filtered = [
            c for c in candidates if c.context_limit >= required_tokens and c.enabled
        ]
        if not filtered:
            filtered = [c for c in candidates if c.enabled]

        if constraints and constraints.max_cost_usd is not None:
            affordable = [
                c
                for c in filtered
                if self._estimated_request_cost(c, features) <= constraints.max_cost_usd
            ]
            if affordable:
                filtered = affordable

        ranked = sorted(filtered, key=lambda c: self._rank_key(c, features, constraints))
        if not ranked:
            raise ValueError("No model candidates available for selection")

        return SelectionResult(primary_candidate=ranked[0], fallback_candidates=ranked[1:])

    def _rank_key(
        self, candidate: ModelCandidate, features: PromptFeatures, constraints: RoutingConstraints | None
    ) -> tuple:
        reliability = self._reliability_store.get(candidate.provider, candidate.model_name)
        blended_cost = (0.6 * candidate.input_cost_per_1m) + (0.4 * candidate.output_cost_per_1m)
        tag_penalty = self._tag_penalty(candidate, features)
        coding_preference_rank = self._coding_preference_rank(candidate, features)

        provider_penalty = 1
        if constraints and constraints.preferred_provider:
            if candidate.provider.lower() == constraints.preferred_provider.lower():
                provider_penalty = 0

        return (
            -reliability,
            coding_preference_rank,
            tag_penalty,
            blended_cost,
            provider_penalty,
            -candidate.context_limit,
        )

    def _estimated_request_cost(self, candidate: ModelCandidate, features: PromptFeatures) -> float:
        prompt_tokens = features.token_estimate + features.context_token_estimate
        if features.has_code or features.has_analysis or features.has_math:
            completion_tokens = max(int(features.token_estimate * 0.8), 200)
        else:
            completion_tokens = max(int(features.token_estimate * 0.5), 80)

        return (
            (prompt_tokens * candidate.input_cost_per_1m)
            + (completion_tokens * candidate.output_cost_per_1m)
        ) / 1_000_000

    def _tag_penalty(self, candidate: ModelCandidate, features: PromptFeatures) -> int:
        tags = {t.lower() for t in candidate.tags}
        penalty = 0

        needs_coding = self._is_coding_task(features)
        needs_reasoning = (
            features.has_math
            or features.has_analysis
            or features.needs_accuracy
            or features.intent == "analysis"
        )
        short_simple = (
            features.intent in {"rewrite", "summarize", "bullets", "brainstorm"}
            and features.token_estimate < 700
            and not needs_coding
            and not needs_reasoning
        )
        large_context = (features.token_estimate + features.context_token_estimate) >= 2200

        if needs_coding and not ({"coding", "reasoning"} & tags):
            penalty += 3
        if needs_reasoning and "reasoning" not in tags:
            penalty += 2
        if short_simple and "non_reasoning" not in tags and "cheap" not in tags:
            penalty += 1
        if large_context and "long_context" not in tags:
            penalty += 1

        return penalty

    def _is_coding_task(self, features: PromptFeatures) -> bool:
        return features.has_code or features.has_logs_stacktrace or features.intent == "code"

    def _coding_preference_rank(self, candidate: ModelCandidate, features: PromptFeatures) -> int:
        """
        Prefer the current, selectable coding-capable models for coding prompts.
        Lower rank value is better. The order comes from
        ``routing_defaults.coding_preference`` in config/model_registry.yaml.
        """
        if not self._is_coding_task(features):
            return 999

        if candidate.coding_preference_rank is not None:
            return candidate.coding_preference_rank

        tags = {t.lower() for t in candidate.tags}
        if {"coding", "reasoning"} & tags:
            return 50
        return 100
