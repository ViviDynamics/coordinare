"""095: infrastructure/environment failure-signature matching (ENV_BLOCKED).

A failing CI check whose normalized reason matches an infra/environment pattern
is one no code change can fix — an exhausted artifact-storage quota, an offline
runner, a billing/spending-limit block, etc. ``match_env_signature`` maps such a
reason to an :class:`EnvCause` (the operator-facing cause + suggested action),
or ``None`` when nothing matches (fail-safe: an unrecognized failure is never
labeled ENV_BLOCKED — FR-003).

Pure and deterministic; matches against the already-lowercased, drift-stripped
normalized reason produced by ``failure_signature.normalize_reason``.

368: Model-based judgment augments regex matching. ``match_env_signature_with_model``
makes an async LLM call to classify environmental failures when the regex patterns
don't match (fail-safe: falls back to regex results if model call fails).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from coordinare.config import EnvSignaturePattern
    from coordinare.services.conducting import ConductingBackend


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
        "registry_auth",
        r"login attempt to .* failed with status: (401|403)|"
        r"(pull|push|registry|docker login).*(unauthorized|authentication required|403 forbidden|401 unauthorized)",
        "CI registry authentication failed",
        "Check runner registry credentials and registry/proxy access policy; repository changes cannot repair runner access",
    ),
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
    (
        # 118: self-hosted-runner setup/toolchain-cache permission failure. The
        # setup-* actions (setup-ruby/node/python) die at the tool-cache step with
        # "EACCES: permission denied, mkdir '/opt/hostedtoolcache'" when the job's
        # effective UID can't write the cache dir — a pure infra failure (the job
        # dies before any project code runs), so it must HOLD, not bounce the
        # implementer. Matches the hostedtoolcache path, the generic tool-cache
        # EACCES, and AGENT_TOOLSDIRECTORY/RUNNER_TOOL_CACHE permission variants.
        # Require BOTH a permission/error qualifier AND a GitHub-Actions-specific
        # tool-cache token, within the same line (≤80 chars apart), in either order.
        # A bare 'hostedtoolcache' mention is NOT enough — setup-* SUCCESS logs name
        # the cache dir ("Found hostedtoolcache for ruby 3.2.0") and must not be held
        # as infra. Generic tokens like "tool cache" are excluded to avoid matching
        # app-level permission errors; only GH-runner-specific names are used.
        "runner_toolcache_perm",
        r"(eacces|permission denied)[^\n]{0,80}(hostedtoolcache|agent_toolsdirectory|runner_tool_cache)|"
        r"(hostedtoolcache|agent_toolsdirectory|runner_tool_cache)[^\n]{0,80}(eacces|permission denied)",
        "Self-hosted runner tool-cache directory not writable (setup-* action failed)",
        "Fix /opt/hostedtoolcache permissions on the runner image / ARC securityContext "
        "(e.g. chmod 1777, or set AGENT_TOOLSDIRECTORY to a writable path)",
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


async def match_env_signature_with_model(
    reason: str,
    patterns: list[EnvSignaturePattern],
    backend: ConductingBackend | None = None,
) -> EnvCause | None:
    """Classify environmental failures using model judgment when regex doesn't match.

    (368) First tries the regex-based ``match_env_signature``. If it matches,
    returns immediately (fast path). If no regex match and a model backend is
    available, makes an async LLM call to judge whether the failure is
    environmental. Falls back to None (fail-safe) if the model call fails or
    times out.

    This is a daemon-friendly variant: it respects timeouts, handles failures
    gracefully, and never blocks other cards. The downstream gates (CI must pass,
    reviewer must approve, security must pass) already validate outcomes, so
    conservative failures are safe.

    Args:
        reason: the normalized failure reason (already lowercased/drift-stripped)
        patterns: operator-supplied env signature patterns
        backend: the conducting backend (async model service); if None or model
            call fails, falls back to regex-only classification

    Returns:
        EnvCause if the failure is environmental, else None (fail-safe).
    """
    # Fast path: regex patterns match
    regex_result = match_env_signature(reason, patterns)
    if regex_result is not None:
        return regex_result

    # Slow path: no regex match, try model if available
    if backend is None:
        return None

    try:
        prompt = (
            "You are classifying CI failures as environmental or not.\n\n"
            "Environmental failures are infrastructure/operator issues that no code change can fix:\n"
            "  - Storage quota exhausted (artifact storage, disk space)\n"
            "  - Runner offline or unavailable\n"
            "  - Billing/spending limits\n"
            "  - Registry/authentication failures\n"
            "  - Permission/access issues (hostedtoolcache, Docker, etc.)\n"
            "  - Network/connectivity issues\n\n"
            "Code failures are issues the implementer can fix:\n"
            "  - Test assertion failures\n"
            "  - Syntax errors\n"
            "  - Logic errors\n"
            "  - Missing dependencies (if the repo can install them)\n"
            "  - Lint/style violations\n\n"
            f"Failure reason (normalized):\n{reason}\n\n"
            'Respond ONLY with JSON: {{"is_environmental": bool, "pattern_id": "category_name", "reason": "brief explanation"}}'
        )

        response = await backend.prompt(prompt, response_format="json")
        data = response.get("data")
        if not isinstance(data, dict):
            # Model returned non-JSON or unparseable response
            return None

        is_env = data.get("is_environmental", False)
        pattern_id = str(data.get("pattern_id", "model_judgment")).strip()
        explanation = str(data.get("reason", "")).strip()

        if not is_env:
            return None

        # Environmental failure detected by model; construct EnvCause
        # Use pattern_id from model for tracing, and a generic action
        return EnvCause(
            pattern_id=f"model:{pattern_id}" if pattern_id else "model:environmental",
            cause=f"Environmental/infrastructure failure (model-detected): {explanation}",
            action="Investigate infrastructure status, runner availability, quota limits, or permissions",
        )

    except Exception:
        # Model call failed, timeout, or returned garbage
        # Fall back to regex-only result (already checked above, which is None)
        return None


__all__ = ["EnvCause", "match_env_signature", "match_env_signature_with_model"]
