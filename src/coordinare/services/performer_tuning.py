"""055: Backend-agnostic performer tuning translation.

Converts the generic effort / temperature / max_tokens fields on
PerformerRoleConfig into the payload keys each backend actually accepts.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from coordinare.config import PerformerRoleConfig

logger = structlog.get_logger(__name__)

# Thinking-budget token counts used when translating effort for Anthropic backends.
_EFFORT_THINKING_TOKENS: dict[str, int] = {
    "low": 1_024,
    "medium": 8_192,
    "high": 32_000,
}

_ANTHROPIC_BACKENDS = {"claude", "anthropic", "claude_code"}
_OPENCODE_BACKENDS = {"opencode", "codex"}


def translate_tuning(role_config: PerformerRoleConfig) -> dict[str, Any]:
    """Return backend-specific payload keys for the tuning fields on *role_config*.

    Keys are omitted entirely when the corresponding field is None so that
    callers can safely merge the result into an existing payload without
    overwriting backend defaults.
    """
    result: dict[str, Any] = {}
    backend = role_config.backend

    if role_config.effort is not None:
        if backend in _ANTHROPIC_BACKENDS:
            tokens = _EFFORT_THINKING_TOKENS.get(role_config.effort, 8_192)
            result["thinking"] = {"type": "enabled", "budget_tokens": tokens}
        elif backend in _OPENCODE_BACKENDS:
            result["effort"] = role_config.effort
        else:
            logger.warning(
                "performer_tuning.unknown_backend_effort",
                backend=backend,
                effort=role_config.effort,
            )
            result["effort"] = role_config.effort

    if role_config.temperature is not None:
        result["temperature"] = role_config.temperature

    if role_config.max_tokens is not None:
        result["max_tokens"] = role_config.max_tokens

    return result
