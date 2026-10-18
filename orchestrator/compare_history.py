"""Select provider history without rewriting or summarizing answer content."""

from __future__ import annotations

import re
from typing import Any, Mapping

from config.cache_optimization import compare_selective_history_enabled
from utils.logger import get_logger

logger = get_logger(__name__)


# Ambiguous references retain every answer. Prefer extra context over dropping
# the answer a user may be referring to; this is intentionally conservative.
_SHARED_REFERENCE = re.compile(
    r"\b(?:compar\w*|contrast\w*|differ\w*|agree\w*|disagree\w*|both|all|"
    r"responses?|answers?|models?|results?|options?|recommendations?|suggestions?|"
    r"first|second|third|1st|2nd|3rd|former|latter|above|previous|earlier|"
    r"other|another|alternative|left|right|middle|which|better|best|versus|vs|"
    r"sonnet|opus|haiku|astra|argon|sol|terra|luna|"
    r"this|that|these|those|it|its|they|them|their|"
    r"gpt\w*|openai|gemini|google|claude|anthropic|grok|deepseek)\b",
    re.IGNORECASE,
)


def select_compare_messages(
    messages: list[dict[str, Any]], *, prompt: str, provider: str, model: str
) -> list[dict[str, Any]]:
    """Keep exact own-model answers for ordinary follow-ups with provenance.

    Old clients, cross-answer references, missing model matches and trimmed
    variant metadata retain shared content. Provenance never reaches providers.
    """
    shared = (
        not compare_selective_history_enabled()
        or not provider
        or not model
        or bool(_SHARED_REFERENCE.search(prompt))
        or any(char.isalpha() and ord(char) > 127 for char in prompt)
    )
    result: list[dict[str, Any]] = []
    selected = 0
    for message in messages:
        clean = {key: value for key, value in message.items() if key != "response_variants"}
        variants = message.get("response_variants")
        if not shared and message.get("role") == "assistant" and isinstance(variants, list):
            valid = [item for item in variants if isinstance(item, Mapping)]
            # Also check the untrimmed boundary here; route trimming can remove
            # provenance, and direct internal callers must not bypass its cap.
            if (
                valid
                and len(valid) == len(variants)
                and "\n\n".join(str(item.get("content") or "") for item in valid)
                == message.get("content")
            ):
                own = [
                    item
                    for item in valid
                    if (
                        str(item.get("provider") or "").strip().lower() == provider.strip().lower()
                        and str(item.get("model") or "").strip() == model.strip()
                    )
                ]
                if len(own) == 1:
                    clean["content"] = own[0]["content"]
                    selected += 1
        result.append(clean)
    if any(message.get("response_variants") for message in messages):
        logger.info(
            "Compare history resolved",
            extra={
                "extra_fields": {
                    "event": "compare.history.selected",
                    "provider": provider,
                    "model": model,
                    "selected_own_answers": selected,
                    "original_characters": sum(len(str(m.get("content") or "")) for m in messages),
                    "retained_characters": sum(len(str(m.get("content") or "")) for m in result),
                }
            },
        )
    return result
