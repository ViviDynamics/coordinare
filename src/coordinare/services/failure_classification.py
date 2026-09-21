"""090 US2 (L2) — failure-origin classification.

Layer 2 of spec 090 (Baseline Repair Autonomy) explains *why* each failing HEAD
check is failing by classifying it against a merge-base baseline.  The result is
strictly observe-only — it changes no verdict and no routing (SC-006) — but it
is the foundation L3 needs to repair an *inherited* failure without ever masking
an *introduced* one.

The classifier is **pure**: no I/O, no clock, no RNG (FR-007).  It implements a
strict **source-order** decision table (data-model §5, first match wins):

  1. HEAD conclusion is transient                       → FLAKE      (FR-010)
  2. baseline indeterminate (no base rollup) OR a
     16-char signature collision is detected            → UNKNOWN    (FR-012)
  3. same-name baseline failure but its conclusion is
     transient (a flaky baseline)                       → INTRODUCED (FR-011)
  4. baseline failure stable AND signature matches      → INHERITED  (FR-008)
  5. otherwise (no same-name baseline failure, or a
     stable baseline with a *different* signature)      → INTRODUCED (FR-009)

Stable vs transient is the keystone of anti-masking:

  - **Stable** (INHERITED-eligible): exactly ``{"failure"}``.
  - **Transient** (never INHERITED): ``timed_out, cancelled, neutral, skipped,
    action_required, stale, startup_failure`` — wider than FR-010's named four
    and deliberately distinct from the performer's ``_FAILING_CONCLUSIONS`` (a
    transient HEAD conclusion that the performer still counts as failing must be
    explainable as a FLAKE here without any performer change).

Two deliberate deviations from data-model §5's documented signatures, both
required for correct collision handling and both inert to the rest of the
system:

- ``classify_failure_origin`` takes a third positional ``head_reason``
  parameter.  ``FailedCheckWithSignature`` carries only the two 16-char hashes
  (adding a third field would break its serialization contract — see
  ``test_ci_gate.test_with_signature_serializes_as_failed_check_plus_two_fields``),
  yet collision detection in :func:`compare_signatures` needs the HEAD's
  normalized reason.  The caller already has it from
  ``make_failure_signature`` and threads it in.

- ``baseline_index`` is ``dict[str, BaselineFailure] | None``.  ``None`` means
  the base rollup was indeterminate/unfetchable (row 2 → every failure UNKNOWN,
  never INHERITED — FR-012).  An **empty dict** means the base WAS fetched and
  had zero failures (row 5 → INTRODUCED).  Conflating the two would either mask
  introduced failures or refuse to ever inherit, so they stay distinct.

``BaselineFailure`` likewise carries ``normalized_reason`` beyond data-model
§5's documented ``(name, conclusion, signature)`` so the classifier can pass the
baseline reason into :func:`compare_signatures` for collision detection.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from coordinare.services.ci_gate import FailedCheckWithSignature, compare_signatures

if TYPE_CHECKING:
    from coordinare.config import EnvSignaturePattern

Classification = Literal["env_blocked", "inherited", "introduced", "flake", "unknown"]

# The only INHERITED-eligible conclusion.  Everything else that can appear on a
# failing check is transient and can never be inherited (it is FLAKE when on the
# HEAD, or forces INTRODUCED when on the baseline).
_STABLE_CONCLUSION = "failure"

# Transient conclusions (data-model §5).  Intentionally wider than FR-010's
# named four (timed_out, cancelled, neutral, action_required) and distinct from
# the performer's ``_FAILING_CONCLUSIONS`` — classification owns its own set so
# no performer change is needed.
_TRANSIENT_CONCLUSIONS: frozenset[str] = frozenset(
    {
        "timed_out",
        "cancelled",
        "neutral",
        "skipped",
        "action_required",
        "stale",
        "startup_failure",
    },
)


class BaselineFailure(BaseModel):
    """A failing check observed on the merge-base baseline rollup.

    ``normalized_reason`` is carried alongside the ``(name, conclusion,
    signature)`` triple so the classifier can feed it to
    :func:`coordinare.services.ci_gate.compare_signatures` for 16-char-truncation
    collision detection — without it a collision could masquerade as an
    inherited match (FR-009).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    conclusion: str
    signature: str
    normalized_reason: str


def _is_transient(conclusion: str) -> bool:
    """True for any conclusion that can never be INHERITED.

    Only the literal ``"failure"`` is stable; an unrecognized conclusion is
    *not* transient (it falls through to the no-baseline / signature rows rather
    than being treated as a flake).
    """
    return conclusion in _TRANSIENT_CONCLUSIONS


def classify_failure_origin(
    head_check: FailedCheckWithSignature,
    head_reason: str,
    baseline_index: dict[str, BaselineFailure] | None,
    env_patterns: list[EnvSignaturePattern] | None = None,
) -> Classification:
    """Classify one failing HEAD check against the baseline (source-order table).

    Args:
        head_check: the failing HEAD check, carrying its 16-char signature.
        head_reason: the HEAD failure's normalized reason (from
            ``make_failure_signature``), needed for collision detection.
        baseline_index: name → :class:`BaselineFailure` for the merge-base
            rollup, or ``None`` when that rollup was indeterminate/unfetchable.

    Returns:
        One of ``"env_blocked" | "inherited" | "introduced" | "flake" | "unknown"``.
        ``"env_blocked"`` is the Row-0 short-circuit (095) — returned only when
        ``env_patterns`` is not None and the HEAD reason matches an infra pattern.
    """
    # Row 0 (095) — an infrastructure/environment failure (matched on the HEAD
    # reason alone) is ENV_BLOCKED: no code change can fix it, so it short-circuits
    # the whole inherited/introduced/flake decision (FR-001, FR-011). ``env_patterns
    # is None`` means the gate is off → skip entirely (FR-012, byte-identical
    # baseline). An empty list means the gate is on with built-ins only.
    if env_patterns is not None:
        from coordinare.services.env_signature import match_env_signature

        if match_env_signature(head_reason, env_patterns) is not None:
            return "env_blocked"

    # Row 1 — a transient HEAD conclusion is a FLAKE regardless of any baseline.
    if _is_transient(head_check.conclusion):
        return "flake"

    # Row 2a — no base rollup at all: we cannot prove the failure pre-existed, so
    # it is UNKNOWN, never INHERITED (FR-012).
    if baseline_index is None:
        return "unknown"

    baseline = baseline_index.get(head_check.name)

    # Row 5 (early) — base was fetched but has no same-name failure: INTRODUCED.
    # Handled before the signature comparison so ``baseline`` is never None below.
    if baseline is None:
        return "introduced"

    comparison = compare_signatures(
        head_check.head_signature,
        head_reason,
        baseline.signature,
        baseline.normalized_reason,
    )

    # Row 2b — equal 16-char hash but different reason is a truncation collision.
    if comparison == "collision":
        return "unknown"

    # Row 3 — a flaky (transient) baseline failure cannot anchor an inheritance;
    # a stable HEAD failure sharing its name is INTRODUCED (FR-011).
    if _is_transient(baseline.conclusion):
        return "introduced"

    # Row 4 — stable baseline AND matching signature → INHERITED (FR-008).
    if comparison == "match":
        return "inherited"

    # Row 5 — stable baseline with a *different* signature → INTRODUCED: HEAD is
    # failing for a different reason than the baseline (anti-masking, FR-009).
    return "introduced"


__all__ = [
    "BaselineFailure",
    "Classification",
    "classify_failure_origin",
]
