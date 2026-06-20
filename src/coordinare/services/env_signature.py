"""095: infrastructure/environment failure-signature matching (ENV_BLOCKED).

A failing CI check whose normalized reason matches an infra/environment pattern
is one no code change can fix — an exhausted artifact-storage quota, an offline
runner, a billing/spending-limit block, etc. ``match_env_signature`` maps such a
reason to an :class:`EnvCause` (the operator-facing cause + suggested action),
or ``None`` when nothing matches (fail-safe: an unrecognized failure is never
labeled ENV_BLOCKED — FR-003).

Pure and deterministic; matches against the already-lowercased, drift-stripped
normalized reason produced by ``failure_signature.normalize_reason``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coordinare.config import EnvSignaturePattern


@dataclass(frozen=True)
class EnvCause:
    """The resolved infra cause for an ENV_BLOCKED failure (no secret values)."""

    pattern_id: str
    cause: str
    action: str


# Built-in infra patterns, matched in order before any operator patterns. Each
# regex is applied case-insensitively to the normalized reason. Reasons (cause /
# action) are operator-facing; they carry no secret values.
_BUILTIN_PATTERNS: tuple[tuple[str, str, str, str], ...] = (
    (
        "artifact_storage_quota",
        r"artifact storage quota|createartifact.*quota|storage quota has been hit",
        "CI artifact-storage quota exhausted",
        "Raise the Actions storage budget (under Actions, not Packages) or clear old artifacts",
    ),
    (
        "runner_offline",
        r"no runner|runner.*offline|waiting for a runner|no.*runner came online|"
        r"cancelled because.*runner",
        "No CI runner available",
        "Bring a self-hosted runner online or check runner registration",
    ),
    (
        "billing_limit",
        r"spending limit|billing|payment.*(failed|required)|exceeded.*included",
        "CI blocked by a billing / spending limit",
        "Raise the spending limit or update the payment method",
    ),
)

_BUILTIN_COMPILED: tuple[tuple[str, re.Pattern[str], str, str], ...] = tuple(
    (pid, re.compile(rx, re.IGNORECASE), cause, action)
    for pid, rx, cause, action in _BUILTIN_PATTERNS
)


def match_env_signature(
    reason: str, patterns: list[EnvSignaturePattern]
) -> EnvCause | None:
    """Return the :class:`EnvCause` for the first matching infra pattern, else None.

    Built-ins are checked first, then operator-supplied ``patterns`` (so the
    shipped set always applies; operators only add). A non-match returns ``None``
    — an unrecognized failure is never ENV_BLOCKED (FR-003, SC-005).
    """
    if not reason:
        return None
    for pid, rx, cause, action in _BUILTIN_COMPILED:
        if rx.search(reason):
            return EnvCause(pattern_id=pid, cause=cause, action=action)
    for pat in patterns or []:
        try:
            if re.search(pat.regex, reason, re.IGNORECASE):
                return EnvCause(pattern_id=pat.id, cause=pat.cause, action=pat.action)
        except re.error:
            # A malformed operator regex must not crash classification — skip it
            # (fail-safe: treat as no-match).
            continue
    return None


__all__ = ["EnvCause", "match_env_signature"]
