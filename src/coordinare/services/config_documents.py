from __future__ import annotations

from typing import Any

import yaml

from coordinare.config import expand_config_text


def _reference_pair(value: Any, index: int, raw: list[Any], expanded: list[Any]) -> tuple[Any, Any]:
    if isinstance(value, dict):
        for key in ("name", "id"):
            if key in value:
                return (
                    next((item for item in raw if isinstance(item, dict) and item.get(key) == value[key]), None),
                    next((item for item in expanded if isinstance(item, dict) and item.get(key) == value[key]), None),
                )
    return (
        raw[index] if index < len(raw) else None,
        expanded[index] if index < len(expanded) else None,
    )


def _keep_references(proposed: Any, raw: Any, expanded: Any) -> Any:
    """Restore raw placeholders when the submitted model still has their loaded value."""
    if isinstance(raw, str) and "${" in raw and proposed == expanded:
        return raw
    if isinstance(proposed, dict) and isinstance(raw, dict) and isinstance(expanded, dict):
        return {
            key: _keep_references(value, raw.get(key), expanded.get(key))
            for key, value in proposed.items()
        }
    if isinstance(proposed, list) and isinstance(raw, list) and isinstance(expanded, list):
        return [
            _keep_references(value, *_reference_pair(value, index, raw, expanded))
            for index, value in enumerate(proposed)
        ]
    return proposed


def symphony_documents(configs: dict[str, Any], raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Serialize symphonies without copying expanded secrets out of daemon memory.

    Match by symphony name so removal or reordering cannot borrow another
    symphony's placeholders. Keep explicit changes and nulls as submitted.
    """
    symphonies = raw.get("symphonies") or []
    stored = {item["name"]: item for item in symphonies if isinstance(item, dict) and "name" in item}
    result: list[dict[str, Any]] = []
    for name, config in configs.items():
        document = config.model_dump(exclude_none=True)
        previous = stored.get(name, {})
        expanded = yaml.safe_load(expand_config_text(yaml.safe_dump(previous)))
        result.append(_keep_references(document, previous, expanded))
    return result
