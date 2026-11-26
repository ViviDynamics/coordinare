"""Structured scope input and explicit backend budget capability checks (#273)."""
from __future__ import annotations

import json
import os


#: Backends that enforce ``max_tool_calls`` themselves: nare natively with
#: ``--max-tool-calls`` (#360), claude_code through a POSIX hook (#273).
_NATIVE_CAP_BACKENDS = frozenset({"nare"})


def _enforces_cap(backend: str) -> bool:
    return backend in _NATIVE_CAP_BACKENDS or (backend == "claude_code" and os.name == "posix")


def scope_prompt_section(score, backend: str) -> list[str]:
    limit = getattr(score, "max_tool_calls", None)
    if limit is not None and not _enforces_cap(backend):
        raise ValueError(f"max_tool_calls is unsupported by {getattr(score, 'backend', None) or backend}; use nare, or claude_code on POSIX, or omit the cap")
    fields = {key: getattr(score, key, "") for key in ("scope_focus", "scope_addon") if getattr(score, key, "")}
    if limit is not None:
        fields["max_tool_calls"] = limit
    if not fields:
        return []
    return ["", "## Persona scope (structured dispatch input)", "", json.dumps(fields, ensure_ascii=True), ""]
