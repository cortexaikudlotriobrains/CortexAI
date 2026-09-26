from dataclasses import replace
from typing import Any

from models.user_context import UserContext
from orchestrator.model_registry import ModelRegistry
from orchestrator.model_selector import ModelSelector
from orchestrator.prompt_analyzer import PromptAnalyzer
from orchestrator.response_validator import ResponseValidator
from orchestrator.routing_types import ModelCandidate, PromptFeatures, RoutingConstraints, Tier
from orchestrator.tier_decider import TierDecider
from utils.token_estimation import estimate_tokens


class SmartRouter:
    def __init__(
        self,
        *,
        registry: ModelRegistry,
        selector: ModelSelector,
        validator: ResponseValidator,
        fallback_manager: Any,
        analyzer: PromptAnalyzer,
        decider: TierDecider,
    ):
        self._registry = registry
        self._selector = selector
        self._validator = validator
        self._fallback_manager = fallback_manager
        self._analyzer = analyzer
        self._decider = decider

    def route_once_plan(
        self,
        prompt: str,
        context: UserContext | None,
        routing_mode: str,
        constraints: RoutingConstraints | None,
        runtime_messages: list[dict[str, str]] | None = None,
    ) -> tuple[PromptFeatures, Tier, list[ModelCandidate], dict[str, Any]]:
        features = self._analyzer.analyze(prompt, context)
        features = self._apply_runtime_message_features(features, runtime_messages)
        features = self._apply_constraints(features, constraints)

        forced_tier = self._resolve_forced_tier(routing_mode)
        if forced_tier:
            tier = forced_tier
            reasons = [f"forced_mode_{routing_mode}"]
        else:
            decision = self._decider.decide(features)
            tier = decision.tier
            reasons = decision.reasons

        candidates = self._registry.get_candidates(tier, constraints)
        if not candidates and constraints and constraints.allowed_models:
            for model_key in constraints.allowed_models:
                provider, separator, model = str(model_key or "").partition(":")
                if not separator:
                    continue
                candidate = self._registry.find_model(provider, model)
                if candidate is None or not candidate.enabled:
                    continue
                tier = candidate.tier
                candidates = self._registry.get_candidates(tier, constraints)
                if candidates:
                    reasons.append("credit_affordable_lower_tier")
                    break
        if not candidates:
            for compatible_tier in (Tier.T0, Tier.T1, Tier.T2, Tier.T3):
                if compatible_tier == tier:
                    continue
                candidates = self._registry.get_candidates(compatible_tier, constraints)
                if candidates:
                    tier = compatible_tier
                    reasons.append("reasoning_capability_tier_adjustment")
                    break
        selection = self._selector.select(features, candidates, constraints)
        ordered_candidates = [selection.primary_candidate, *selection.fallback_candidates]

        metadata = self.make_metadata(
            mode=routing_mode,
            initial_tier=tier,
            final_tier=tier,
            decision_reasons=reasons,
            ordered_candidates=ordered_candidates,
            features=features,
            prompt=prompt,
        )
        metadata["selection"] = {
            "primary_provider": selection.primary_candidate.provider,
            "primary_model": selection.primary_candidate.model_name,
            "primary_blended_cost_per_1m": round(
                (0.6 * selection.primary_candidate.input_cost_per_1m)
                + (0.4 * selection.primary_candidate.output_cost_per_1m),
                6,
            ),
            "fallback_count": len(selection.fallback_candidates),
        }

        return features, tier, ordered_candidates, metadata

    def make_metadata(
        self,
        *,
        mode: str,
        initial_tier: Tier,
        final_tier: Tier,
        decision_reasons: list[str],
        ordered_candidates: list[ModelCandidate] | None = None,
        features: PromptFeatures | None = None,
        prompt: str = "",
    ) -> dict[str, Any]:
        candidate_plan = self._build_candidate_plan(
            ordered_candidates=ordered_candidates or [],
            features=features,
            tier=initial_tier,
        )

        selected_slots = self._build_selected_slots(candidate_plan)
        features_payload = vars(features) if features else {}
        prompt_category = self._derive_prompt_category(prompt=prompt, features=features)

        return {
            "mode": mode,
            "initial_tier": initial_tier.value,
            "final_tier": final_tier.value,
            "attempt_count": 0,
            "fallback_used": False,
            "attempts": [],
            "decision_reasons": decision_reasons,
            "candidate_plan": candidate_plan,
            "selected_sequence": [],
            "first_selected_model": selected_slots[0],
            "second_selected_model": selected_slots[1],
            "third_selected_model": selected_slots[2],
            "features": features_payload,
            "prompt_category": prompt_category,
        }

    @staticmethod
    def _estimate_text_tokens(text: str) -> int:
        return estimate_tokens(text)

    def _apply_runtime_message_features(
        self,
        features: PromptFeatures,
        runtime_messages: list[dict[str, str]] | None,
    ) -> PromptFeatures:
        """
        Adjust prompt/context token estimates using the full runtime message payload.

        This captures system prompts and current-turn web research injections that are
        not represented in plain `prompt + conversation_history` analysis.
        """
        if not runtime_messages:
            return features

        total_runtime_tokens = 0
        last_user_tokens = 0
        for msg in runtime_messages:
            content = str(msg.get("content", "") or "")
            role = str(msg.get("role", "") or "").strip().lower()
            msg_tokens = self._estimate_text_tokens(content)
            total_runtime_tokens += msg_tokens
            if role == "user":
                last_user_tokens = msg_tokens

        if total_runtime_tokens <= 0:
            return features

        prompt_tokens = max(last_user_tokens, features.token_estimate)
        context_tokens = max(total_runtime_tokens - prompt_tokens, 0)
        context_messages = max(len(runtime_messages) - 1, 0)

        return replace(
            features,
            token_estimate=max(features.token_estimate, prompt_tokens),
            context_token_estimate=max(features.context_token_estimate, context_tokens),
            context_messages=max(features.context_messages, context_messages),
        )

    def _build_candidate_plan(
        self,
        *,
        ordered_candidates: list[ModelCandidate],
        features: PromptFeatures | None,
        tier: Tier,
    ) -> list[dict[str, Any]]:
        if not ordered_candidates:
            return []

        token_buffer = int(getattr(self._selector, "_token_buffer", 200))
        required_tokens = 0
        if features:
            required_tokens = (
                int(features.token_estimate) + int(features.context_token_estimate) + token_buffer
            )

        plan: list[dict[str, Any]] = []
        for index, candidate in enumerate(ordered_candidates, start=1):
            blended_cost = round(
                (0.6 * candidate.input_cost_per_1m) + (0.4 * candidate.output_cost_per_1m), 6
            )
            why_selected = [
                f"ranked_{index}_by_selector_within_{tier.value}",
                f"blended_cost_per_1m={blended_cost}",
            ]

            if required_tokens > 0:
                if candidate.context_limit >= required_tokens:
                    why_selected.append(
                        f"context_limit_ok_{candidate.context_limit}_for_required_{required_tokens}"
                    )
                else:
                    why_selected.append(
                        f"context_limit_below_required_{candidate.context_limit}_lt_{required_tokens}_kept_as_fallback"
                    )

            tags = {t.lower() for t in candidate.tags}
            if features:
                if features.has_code:
                    if {"coding", "reasoning"} & tags:
                        why_selected.append("matches_code_or_reasoning_tags")
                    else:
                        why_selected.append("selected_despite_weak_code_tags_as_available_fallback")
                if features.has_analysis or features.has_math or features.needs_accuracy:
                    if "reasoning" in tags:
                        why_selected.append("matches_reasoning_requirement")
                    else:
                        why_selected.append(
                            "selected_without_reasoning_tag_due_to_ranking_availability"
                        )
                if (features.token_estimate + features.context_token_estimate) >= 2200:
                    if "long_context" in tags:
                        why_selected.append("long_context_preferred")
                    else:
                        why_selected.append("long_context_not_tagged")

            plan.append(
                {
                    "order": index,
                    "provider": candidate.provider,
                    "model": candidate.model_name,
                    "tier": tier.value,
                    "billing_class": candidate.billing_class.value,
                    "status": "pending",
                    "outcome_reason": "not_attempted",
                    "why_selected": why_selected,
                }
            )

        return plan

    def _build_selected_slots(
        self, candidate_plan: list[dict[str, Any]]
    ) -> list[dict[str, Any] | None]:
        slots: list[dict[str, Any] | None] = []
        for idx in range(3):
            if idx < len(candidate_plan):
                item = candidate_plan[idx]
                slots.append(
                    {
                        "order": item["order"],
                        "provider": item["provider"],
                        "model": item["model"],
                        "tier": item["tier"],
                        "billing_class": item["billing_class"],
                        "why_selected": item.get("why_selected", []),
                        "status": "pending",
                        "why_worked": None,
                        "why_failed": None,
                    }
                )
            else:
                slots.append(None)
        return slots

    def _derive_prompt_category(self, prompt: str, features: PromptFeatures | None) -> str:
        """
        Derive prompt category aligned to DB enum:
        coding|financial|educational|math|legal|data_technical|general|unknown
        """
        text = (prompt or "").lower()
        has_code = bool(features and features.has_code)
        has_math = bool(features and features.has_math)
        has_analysis = bool(features and features.has_analysis)
        has_logs = bool(features and features.has_logs_stacktrace)
        intent = features.intent if features else "general"

        if has_code or intent == "code":
            return "coding"
        if has_math:
            return "math"
        if any(
            kw in text
            for kw in [
                "stock",
                "stocks",
                "market",
                "finance",
                "financial",
                "revenue",
                "profit",
                "portfolio",
                "crypto",
                "investment",
                "nasdaq",
                "dow jones",
            ]
        ):
            return "financial"
        if any(
            kw in text
            for kw in ["law", "legal", "contract", "liability", "regulation", "statute", "court"]
        ):
            return "legal"
        if any(
            kw in text
            for kw in [
                "teach",
                "lesson",
                "tutorial",
                "explain like",
                "beginner",
                "homework",
                "student",
                "study plan",
            ]
        ):
            return "educational"
        if has_analysis or intent == "analysis" or has_logs:
            return "data_technical"
        if prompt.strip():
            return "general"
        return "unknown"

    def _apply_constraints(
        self, features: PromptFeatures, constraints: RoutingConstraints | None
    ) -> PromptFeatures:
        if not constraints:
            return features

        strict = features.strict_format
        has_strict = features.has_strict_constraints
        if constraints.strict_format or constraints.json_only:
            strict = True
            has_strict = True

        return PromptFeatures(
            word_count=features.word_count,
            char_count=features.char_count,
            token_estimate=features.token_estimate,
            has_code=features.has_code,
            has_math=features.has_math,
            has_analysis=features.has_analysis,
            has_creative=features.has_creative,
            has_factual=features.has_factual,
            strict_format=strict,
            has_logs_stacktrace=features.has_logs_stacktrace,
            context_token_estimate=features.context_token_estimate,
            context_messages=features.context_messages,
            is_follow_up=features.is_follow_up,
            needs_latest_info=features.needs_latest_info,
            needs_accuracy=features.needs_accuracy,
            intent=features.intent,
            has_strict_constraints=has_strict,
        )

    def _resolve_forced_tier(self, routing_mode: str) -> Tier | None:
        if routing_mode == "cheap":
            return Tier.T0
        if routing_mode == "strong":
            return Tier.T2
        return None
