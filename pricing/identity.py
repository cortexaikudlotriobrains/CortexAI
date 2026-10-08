"""One exact catalog mapping layer. Never infer prices from a model-name prefix."""

from pathlib import Path
from typing import Any

import yaml

CATALOG_PATH = Path(__file__).resolve().parents[1] / "config" / "model_registry.yaml"
PROVIDERS = {
    "openai": "openai",
    "anthropic": "claude",
    "gemini": "gemini",
    "xai": "grok",
    "deepseek": "deepseek",
}


class ModelIdentityMap:
    def __init__(self, path: Path = CATALOG_PATH):
        self.catalog: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.mapping: dict[tuple[str, str], str] = {}
        self.models: dict[tuple[str, str], dict[str, Any]] = {}
        for provider, block in self.catalog["providers"].items():
            for model in block["models"]:
                canonical = str(model["name"])
                self.models[provider, canonical] = model
                for external in [
                    canonical,
                    *(model.get("aliases") or []),
                    *(model.get("pricing_source_ids") or []),
                ]:
                    keys = [str(external)]
                    for source_provider, cortex_provider in PROVIDERS.items():
                        if cortex_provider == provider:
                            keys.append(f"{source_provider}/{external}")
                    for key in keys:
                        existing = self.mapping.get((provider, key))
                        if existing and existing != canonical:
                            raise ValueError(f"Ambiguous pricing identity: {provider}:{key}")
                        self.mapping[provider, key] = canonical

    def resolve(self, provider: str, external: str) -> str | None:
        return self.mapping.get((provider, external))
