from __future__ import annotations

import re
from typing import Any

from coordinare.config import ProjectConfiguration
from coordinare.env_placeholders import is_secret_env_key
from coordinare.start_checks import _values_at, checked_fields

_PLACEHOLDER = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


def persistent_secret_problem(raw: dict[str, Any]) -> str | None:
    """Require credential indirection in persisted config without echoing a value."""
    documents = [raw]
    symphonies = raw.get("symphonies", [])
    if isinstance(symphonies, list):
        documents.extend(
            symphony["overrides"] for symphony in symphonies
            if isinstance(symphony, dict) and isinstance(symphony.get("overrides"), dict)
        )
    for document in documents:
        for path, rule in checked_fields(ProjectConfiguration):
            if rule not in {"secret", "env"}:
                continue
            for _display, value, key in _values_at(document, path, ""):
                if rule == "env" and not is_secret_env_key(key):
                    continue
                if value is None or value == "":
                    continue
                if isinstance(value, str) and _PLACEHOLDER.fullmatch(value):
                    continue
                return (
                    "Persisted config secrets must use a ${VAR} placeholder or null. "
                    "Supply credentials through the Kubernetes Secret."
                )
    return None
