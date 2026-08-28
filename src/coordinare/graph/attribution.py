"""Standardized PR/issue comment attribution (spec 077).

Every coordinare and performer comment posts as the same GitHub app
('vivi-coordinare'), so on its own a comment gives no hint of *who* spoke, with
*which* agent harness, driving *which* model backend — the central question of a
multi-backend round.  This module builds the canonical attribution header for
**coordinare-origin** comments; the performer side emits a byte-compatible header
from ``performer.main._attribution_header``.  Keep the two in sync (the
``test_attribution`` suite asserts the formats match).

Header shape (two lines): a hidden machine-readable marker followed by a visible
blockquote line.

    <!-- coordinare-attribution origin=coordinare role=implementing harness=codex model=local/qwen3.6:35b -->
    > 🎼 **Coordinare** · re: Implementer (`codex` · `local/qwen3.6:35b`)

Per the operator's design decision, a coordinare comment **tags the stage it acted
on**: it resolves that stage's harness/model from config and references them, so
"the coordinare bounced the implementer's CI" reads with the implementer's
backend, not a generic coordinare identity.
"""

from __future__ import annotations

from typing import Any

# Stage key -> human display label. Mirrors performer.main._ROLE_DISPLAY so the
# visible text is identical regardless of which unit posted the comment.
_STAGE_DISPLAY: dict[str, str] = {
    "assessing": "Assessor",
    "architecting": "Architect",
    "implementing": "Implementer",
    "reviewing": "Reviewer",
    "closing_review": "Closer",
    "security": "Security",
    "qa": "QA",
    "documenting": "Tech Writer",
    "env_bootstrap": "Env Bootstrap",
}

# Stage key -> performer role name (the key into PerformersConfig). Mirrors
# dispatch_performer._STAGE_TO_ROLE for the stages that map to a configured role.
_STAGE_TO_ROLE: dict[str, str] = {
    "assessing": "assessor",
    "architecting": "architect",
    "implementing": "implementer",
    "reviewing": "reviewer",
    "security": "security",
    "qa": "qa",
    "documenting": "tech_writer",
    "closing_review": "closer",
    "env_bootstrap": "env_bootstrap",
}

_UNKNOWN = "?"


def attribution_header(
    *,
    origin: str,
    role: str,
    display: str,
    harness: str,
    model: str,
) -> str:
    """Build a standardized attribution header (marker line + visible blockquote).

    Byte-compatible with ``performer.main._attribution_header``. ``origin`` is
    ``coordinare`` or ``performer``; ``role`` is the raw lifecycle stage key used in
    the machine marker; ``display`` is the human label shown to readers.
    """
    marker = (
        f"<!-- coordinare-attribution origin={origin} role={role} "
        f"harness={harness} model={model} -->"
    )
    if origin == "coordinare":
        # Stage-agnostic coordinare comments (board-level: blocked reminders, dep
        # cycles, escalations) have no harness/model to tag — emit a bare identity
        # rather than a noisy `re: Coordinare (? · ?)` clause.
        if harness == _UNKNOWN and model == _UNKNOWN:
            line = "> 🎼 **Coordinare**"
        else:
            line = f"> 🎼 **Coordinare** · re: {display} (`{harness}` · `{model}`)"
    else:
        line = f"> 🤖 **{display}** · harness `{harness}` · model `{model}`"
    return f"{marker}\n{line}"


def resolve_stage_attribution(config: Any, stage: str | None) -> tuple[str, str, str]:
    """Resolve ``(display, harness, model)`` for a lifecycle ``stage`` from config.

    Falls back to ``"?"`` for harness/model when the stage is unknown/unmapped or
    the role is not configured, so callers always get a usable header.
    """
    display = _STAGE_DISPLAY.get(stage or "", stage or "Coordinare")
    harness = _UNKNOWN
    model = _UNKNOWN
    role = _STAGE_TO_ROLE.get(stage or "")
    performers = getattr(config, "performers", None)
    if role and performers is not None and hasattr(performers, "resolved_role"):
        try:
            role_config = performers.resolved_role(role)
        except Exception:
            role_config = None
        if role_config is not None:
            harness = getattr(role_config, "backend", None) or _UNKNOWN
            model = getattr(role_config, "model", None) or _UNKNOWN
    return display, harness, model


def coordinare_attribution(config: Any, stage: str | None) -> str:
    """Convenience: build the coordinare attribution header for a given ``stage``.

    Resolves the stage's harness/model from ``config`` and tags the stage the
    coordinare acted on. ``stage`` is the lifecycle stage key (e.g. ``implementing``);
    pass ``None`` for stage-agnostic coordinare comments.
    """
    display, harness, model = resolve_stage_attribution(config, stage)
    return attribution_header(
        origin="coordinare",
        role=stage or "coordinare",
        display=display,
        harness=harness,
        model=model,
    )
