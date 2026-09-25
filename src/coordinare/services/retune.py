"""428 — bounded observer retune of coordinare orchestration knobs.

A ``retune`` verdict lets the observer adjust coordinare-side orchestration
knobs mid-session: effort level, the per-turn token budget (``max_tokens``)
and ``max_tool_calls``. The blast radius is bounded by explicit config: only
knobs declared in ``ObserverConfig.retune_bounds`` are adjustable, requests
clamp into their floor and ceiling, and mode switches are refused outright —
retune is not a routing change and never touches catalogs.

Everything here is total and pure: a retune failure leaves the previous value
in place (fail-safe) and never errors the monitoring cycle. The audit record
each decision carries (before, requested, applied) is the structured event a
config diff can be reconstructed from.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

#: The knob vocabulary a retune may ever touch. Anything else — routing,
#: catalogs, mode parameters — is out of scope by construction.
RETUNE_KNOBS = frozenset({"effort", "max_tokens", "max_tool_calls"})

#: Keys that request a mode (strategy) change rather than a knob value.
#: Retune refuses them: strategy changes belong to the operator's config.
MODE_SWITCH_KEYS = frozenset({"mode", "strategy"})

#: Rank order for the effort vocabulary, so a floor/ceiling pair clamps it.
EFFORT_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}

_OUTCOME_APPLIED = "applied"
_OUTCOME_CLAMPED = "clamped"
_OUTCOME_MODE_SWITCH = "refused_mode_switch"
_OUTCOME_NOT_RETUNABLE = "refused_not_retunable"
_OUTCOME_INVALID = "refused_invalid"


@dataclass(frozen=True)
class RetuneDecision:
    """One requested knob's outcome: the audit record of one retune line."""

    knob: str
    before: Any
    requested: Any
    applied: Any
    outcome: str

    @property
    def accepted(self) -> bool:
        return self.outcome in (_OUTCOME_APPLIED, _OUTCOME_CLAMPED)


def _clamp_int(requested: Any, bound: dict[str, Any]) -> int | None:
    """Coerce the request to an int and clamp it into the bound.

    Returns None when the request is not a usable number (fail-safe: the
    previous value stays in place), never an out-of-bound value.
    """
    if isinstance(requested, bool) or not isinstance(requested, (int, float)):
        return None
    if isinstance(requested, float) and not math.isfinite(requested):
        return None  # NaN/Infinity: refused_invalid, not a crash (fail-safe)
    floor = bound.get("floor")
    ceiling = bound.get("ceiling")
    if isinstance(floor, bool) or not isinstance(floor, int):
        return None
    if isinstance(ceiling, bool) or not isinstance(ceiling, int):
        return None
    return max(floor, min(ceiling, int(requested)))


def _clamp_effort(requested: Any, bound: dict[str, Any]) -> str | None:
    """Clamp an effort request into the configured floor/ceiling ranks."""
    rank = EFFORT_RANK.get(requested) if isinstance(requested, str) else None
    if rank is None:
        return None
    raw_floor, raw_ceiling = bound.get("floor"), bound.get("ceiling")
    if not isinstance(raw_floor, str) or not isinstance(raw_ceiling, str):
        return None
    floor = EFFORT_RANK.get(raw_floor)
    ceiling = EFFORT_RANK.get(raw_ceiling)
    if floor is None or ceiling is None:
        return None
    clamped = min(max(rank, floor), ceiling)
    for name, value in EFFORT_RANK.items():
        if value == clamped:
            return name
    return None  # pragma: no cover — EFFORT_RANK covers every rank by construction


def apply_retune(
    current_overrides: dict[str, Any] | None,
    requested_values: Any,
    bounds: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[RetuneDecision]]:
    """Fold one retune verdict's requested values into the override store.

    Returns the NEW override mapping (the previous value for every refused
    knob is retained) and one audit decision per requested knob. Never
    raises: malformed input is a refusal, not an error the cycle.
    """
    current = dict(current_overrides or {})
    requested = requested_values if isinstance(requested_values, dict) else {}
    bound_map = bounds if isinstance(bounds, dict) else {}
    decisions: list[RetuneDecision] = []
    for knob in sorted(requested, key=str):
        value = requested[knob]
        before = current.get(knob)
        if knob in MODE_SWITCH_KEYS:
            decisions.append(RetuneDecision(knob, before, value, None, _OUTCOME_MODE_SWITCH))
            continue
        bound = bound_map.get(knob) if isinstance(bound_map, dict) else None
        if not isinstance(bound, dict):
            decisions.append(RetuneDecision(knob, before, value, None, _OUTCOME_NOT_RETUNABLE))
            continue
        applied = _clamp_effort(value, bound) if knob == "effort" else _clamp_int(value, bound)
        if applied is None:
            decisions.append(RetuneDecision(knob, before, value, None, _OUTCOME_INVALID))
            continue
        outcome = _OUTCOME_APPLIED if applied == value else _OUTCOME_CLAMPED
        current[knob] = applied
        decisions.append(RetuneDecision(knob, before, value, applied, outcome))
    return current, decisions


def retune_prompt_fragment(bounds: dict[str, Any] | None) -> str:
    """The retune evidence line: which knobs are retunable and their bounds.

    Empty when nothing is retunable — the default — so the prompt is
    byte-identical to the pre-428 shape unless retune is explicitly configured.
    """
    if not bounds:
        return ""
    parts: list[str] = []
    for knob in sorted(bounds, key=str):
        bound = bounds[knob]
        if not isinstance(bound, dict):
            continue
        floor, ceiling = bound.get("floor"), bound.get("ceiling")
        if knob == "effort":
            parts.append(f"{knob}=[{floor}..{ceiling}]")
        else:
            parts.append(f"{knob}=[{floor}, {ceiling}]")
    if not parts:
        return ""
    return "Retunable knobs (requests outside a bound are clamped to it): " + ", ".join(parts)
