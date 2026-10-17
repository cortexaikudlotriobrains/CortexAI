"""Text-level edits to config/model_registry.yaml and config/providers.yaml.

The registry is hand-maintained YAML with comments and a consistent quoting style,
so edits are made on the text instead of re-dumping the parsed document. Every
function returns new text and leaves validation to `checks.py`.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import yaml

ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ROOT / "config" / "model_registry.yaml"
PROVIDERS_PATH = ROOT / "config" / "providers.yaml"

_ENTRY_INDENT = "      "  # "      - name:" under providers.<id>.models
_FIELD_INDENT = "        "  # fields of a model entry
_LIFECYCLE_ORDER = (
    "status",
    "retirement_date",
    "alias_target",
    "replacement_model",
    "migration_reason",
)


class RegistryEditError(ValueError):
    pass


# --------------------------------------------------------------------------- locate


def _provider_bounds(lines: list[str], provider: str) -> tuple[int, int]:
    start = next(
        (index for index, line in enumerate(lines) if line.rstrip() == f"  {provider}:"),
        None,
    )
    if start is None:
        raise RegistryEditError(f"Provider '{provider}' not found in the registry")
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line and not line.startswith("   ") and line.strip() and not line.startswith("#"):
            # Next provider ("  gemini:") or a top-level key ("routing_defaults:").
            if re.match(r"^(  )?\S", line):
                end = index
                break
    return start, end


def entry_bounds(lines: list[str], provider: str, name: str) -> tuple[int, int]:
    """Return [start, end) line indexes of one model entry, without trailing blanks."""

    p_start, p_end = _provider_bounds(lines, provider)
    header = f'{_ENTRY_INDENT}- name: "{name}"'
    start = next(
        (i for i in range(p_start, p_end) if lines[i].rstrip() == header),
        None,
    )
    if start is None:
        raise RegistryEditError(f"Model '{provider}:{name}' not found in the registry")
    end = p_end
    for index in range(start + 1, p_end):
        if lines[index].startswith(f"{_ENTRY_INDENT}- name:"):
            end = index
            break
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return start, end


def model_names(text: str, provider: str) -> list[str]:
    lines = text.splitlines()
    p_start, p_end = _provider_bounds(lines, provider)
    names = []
    for line in lines[p_start:p_end]:
        match = re.match(rf'^{_ENTRY_INDENT}- name: "([^"]+)"', line)
        if match:
            names.append(match[1])
    return names


# --------------------------------------------------------------------------- render


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (float, Decimal)):
        number = Decimal(str(value)).normalize()
        text = format(number, "f")
        return text if "." in text else f"{text}.0"
    if value is None:
        return "null"
    return json.dumps(str(value), ensure_ascii=False)


def _inline_list(values: Iterable[Any]) -> str:
    return "[" + ", ".join(_scalar(value) for value in values) + "]"


def _render_mapping(mapping: dict[str, Any], indent: str) -> list[str]:
    lines: list[str] = []
    for key, value in mapping.items():
        if value is None:
            continue
        if isinstance(value, dict):
            lines.append(f"{indent}{key}:")
            lines.extend(_render_mapping(value, indent + "  "))
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            lines.append(f"{indent}{key}:")
            for item in value:
                item_lines = _render_mapping(item, indent + "    ")
                first = item_lines[0].strip()
                lines.append(f"{indent}  - {first}")
                lines.extend(item_lines[1:])
        elif isinstance(value, list):
            lines.append(f"{indent}{key}: {_inline_list(value)}")
        else:
            lines.append(f"{indent}{key}: {_scalar(value)}")
    return lines


def render_entry(entry: dict[str, Any]) -> str:
    """Render one model entry in the registry's house style (6-space list item)."""

    if "name" not in entry:
        raise RegistryEditError("A model entry needs a name")
    body = dict(entry)
    name = body.pop("name")
    lines = [f"{_ENTRY_INDENT}- name: {_scalar(name)}"]
    lines.extend(_render_mapping(body, _FIELD_INDENT))
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- edits


def insert_entry(text: str, provider: str, entry_text: str, *, before: str | None = None) -> str:
    """Insert a rendered entry before another model, or at the top of the provider."""

    lines = text.splitlines(keepends=True)
    plain = [line.rstrip("\n") for line in lines]
    if before:
        start, _ = entry_bounds(plain, provider, before)
    else:
        p_start, p_end = _provider_bounds(plain, provider)
        first = next(
            (i for i in range(p_start, p_end) if plain[i].startswith(f"{_ENTRY_INDENT}- name:")),
            None,
        )
        if first is None:
            raise RegistryEditError(f"Provider '{provider}' has no models list")
        start = first
    block = entry_text if entry_text.endswith("\n") else entry_text + "\n"
    return "".join(lines[:start]) + block + "\n" + "".join(lines[start:])


def _lifecycle_span(plain: list[str], start: int, end: int) -> tuple[int, int] | None:
    for index in range(start, end):
        if plain[index].startswith(f"{_FIELD_INDENT}lifecycle:"):
            stop = index + 1
            while stop < end and plain[stop].startswith(_FIELD_INDENT + "  "):
                stop += 1
            return index, stop
    return None


def _render_lifecycle(lifecycle: dict[str, Any]) -> list[str]:
    ordered = {key: lifecycle[key] for key in _LIFECYCLE_ORDER if lifecycle.get(key)}
    ordered.update({key: value for key, value in lifecycle.items() if key not in ordered and value})
    return [f"{_FIELD_INDENT}lifecycle:"] + [
        f"{_FIELD_INDENT}  {key}: {_scalar(value)}" for key, value in ordered.items()
    ]


def update_lifecycle(text: str, provider: str, name: str, **changes: Any) -> str:
    """Merge lifecycle fields (a value of None removes the key)."""

    lines = text.splitlines(keepends=True)
    plain = [line.rstrip("\n") for line in lines]
    start, end = entry_bounds(plain, provider, name)
    span = _lifecycle_span(plain, start, end)
    if span is None:
        raise RegistryEditError(f"{provider}:{name} has no lifecycle block")
    l_start, l_end = span
    current = yaml.safe_load("\n".join(line[len(_FIELD_INDENT) :] for line in plain[l_start:l_end]))
    lifecycle = dict((current or {}).get("lifecycle") or {})
    for key, value in changes.items():
        if value is None:
            lifecycle.pop(key, None)
        else:
            lifecycle[key] = value
    rendered = [line + "\n" for line in _render_lifecycle(lifecycle)]
    return "".join(lines[:l_start]) + "".join(rendered) + "".join(lines[l_end:])


def set_field(text: str, provider: str, name: str, field: str, value: Any) -> str:
    """Replace a top-level scalar or inline-list field of one entry."""

    lines = text.splitlines(keepends=True)
    plain = [line.rstrip("\n") for line in lines]
    start, end = entry_bounds(plain, provider, name)
    prefix = f"{_FIELD_INDENT}{field}:"
    rendered = (
        f"{prefix} {_inline_list(value)}\n"
        if isinstance(value, list)
        else f"{prefix} {_scalar(value)}\n"
    )
    for index in range(start, end):
        if plain[index].startswith(prefix):
            lines[index] = rendered
            return "".join(lines)
    # Missing field: add it after the name line.
    return "".join(lines[: start + 1]) + rendered + "".join(lines[start + 1 :])


def remove_field(text: str, provider: str, name: str, field: str) -> str:
    lines = text.splitlines(keepends=True)
    plain = [line.rstrip("\n") for line in lines]
    start, end = entry_bounds(plain, provider, name)
    prefix = f"{_FIELD_INDENT}{field}:"
    kept = [
        line
        for index, line in enumerate(lines)
        if not (start <= index < end and plain[index].startswith(prefix))
    ]
    return "".join(kept)


def hide_entry(text: str, provider: str, name: str, *, replacement: str, reason: str) -> str:
    """Take a model out of the picker but keep it callable and priced.

    The provider still serves the model, so its lifecycle status is unchanged;
    `replacement_model` tells History, the API and the frontend what replaced it.
    """

    text = set_field(text, provider, name, "selectable", False)
    return update_lifecycle(
        text,
        provider,
        name,
        replacement_model=replacement,
        migration_reason=reason,
    )


def replace_quoted_in_section(text: str, section: str, old: str, new: str) -> tuple[str, int]:
    """Replace a quoted value inside one top-level section (for example ui_defaults)."""

    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.rstrip() == f"{section}:"), None)
    if start is None:
        return text, 0
    end = next(
        (i for i in range(start + 1, len(lines)) if re.match(r"^\S", lines[i])),
        len(lines),
    )
    count = 0
    for index in range(start, end):
        if f'"{old}"' in lines[index]:
            lines[index] = lines[index].replace(f'"{old}"', f'"{new}"')
            count += 1
    return "".join(lines), count


def replace_provider_default(text: str, provider: str, old: str, new: str) -> tuple[str, int]:
    """Move `default_model` / `compare_default_model` of one provider in providers.yaml."""

    lines = text.splitlines(keepends=True)
    start = next(
        (i for i, line in enumerate(lines) if line.strip() == f"- id: {provider}"),
        None,
    )
    if start is None:
        raise RegistryEditError(f"Provider '{provider}' not found in providers.yaml")
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("- id:")),
        len(lines),
    )
    count = 0
    for index in range(start, end):
        match = re.match(r"^(\s+(?:compare_)?default_model:\s*)(\S+)\s*$", lines[index])
        if match and match[2].strip("\"'") == old:
            lines[index] = f"{match[1]}{new}\n"
            count += 1
    return "".join(lines), count
