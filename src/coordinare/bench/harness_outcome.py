"""Spec 161 — classify one dispatch's terminal marker into an outcome class.

The distinction this module exists for: ``runner._dispatch_status`` collapses *every*
non-success terminal marker into a single ``"failed"`` value, so that bucket contains
both a role rendering a legitimate negative verdict (``changes_requested``) and a harness
that could not produce usable output (``malformed_output``). Ranking harnesses on
``status`` would therefore penalize a reviewer for correctly rejecting bad code, and
would rank a rubber-stamping harness above a discerning one.

The raw ``terminal_marker`` is preserved on the artifact, so the distinction is
recoverable from runs that were already recorded. Recovering it is the point of spec 161.

**Invariant**: classification reads ``terminal_marker``. It MUST NOT read ``status``.

Contract: ``specs/161-board-bench-harness/contracts/outcome-classification.md``.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from coordinare.graph.nodes.monitor_performer import TERMINAL_SUCCESS_STATES

if TYPE_CHECKING:
    from coordinare.bench.artifact import PersonaDispatch


class OutcomeClass(Enum):
    """The four-way classification of one dispatch (spec-161 FR-001).

    ``CREDIT`` and ``HARNESS_DEFECT`` are the *conclusive* classes: rates are computed
    over those two only. ``ENVIRONMENT`` and ``INCONCLUSIVE`` are excluded from both
    numerator and denominator so infrastructure noise never masquerades as signal.
    """

    CREDIT = "credit"
    HARNESS_DEFECT = "harness_defect"
    ENVIRONMENT = "environment"
    INCONCLUSIVE = "inconclusive"


#: Markers where the role rendered a legitimate negative verdict (FR-002). The harness
#: worked and the role said no; that is the role doing its job, so it earns credit.
NEGATIVE_VERDICT_MARKERS: frozenset[str] = frozenset({
    "changes_requested",
    "qa_failed",
    "security_failed",
    "blocked",
})

#: Markers where the harness itself could not produce usable output (FR-003).
HARNESS_DEFECT_MARKERS: frozenset[str] = frozenset({
    "malformed_output",
    "system_error",
})

#: Markers where the environment failed, not the harness or the model (FR-004).
ENVIRONMENT_MARKERS: frozenset[str] = frozenset({"env_blocked"})

#: Conclusive classes — the denominator for every rate this feature reports.
CONCLUSIVE_CLASSES: frozenset[OutcomeClass] = frozenset({
    OutcomeClass.CREDIT,
    OutcomeClass.HARNESS_DEFECT,
})


def credit_markers() -> set[str]:
    """Every marker that earns credit: the **imported** success states plus the four
    negative verdicts.

    ``TERMINAL_SUCCESS_STATES`` is imported rather than copied on purpose. A duplicated
    literal would drift the first time a success marker is added upstream, and the drift
    would be silent: the new success would be reclassified as an unknown marker and land
    in ``INCONCLUSIVE``, quietly shrinking every denominator.
    """
    return set(TERMINAL_SUCCESS_STATES) | set(NEGATIVE_VERDICT_MARKERS)


def _known_markers() -> set[str]:
    return credit_markers() | set(HARNESS_DEFECT_MARKERS) | set(ENVIRONMENT_MARKERS)


def is_unknown_marker(marker: str | None) -> bool:
    """True when *marker* is a non-empty value outside the known vocabulary (FR-006).

    An **absent** marker is not "unknown": it means the dispatch never reached a terminal
    status (budget or teardown cut it off), which is a different thing from a marker the
    vocabulary has not been taught. Both are ``INCONCLUSIVE``, but only the latter is
    worth surfacing as a vocabulary gap.
    """
    if not marker:
        return False
    return marker not in _known_markers()


def classify_marker(marker: str | None) -> OutcomeClass:
    """Classify a raw terminal marker (FR-001 through FR-006).

    Total: every input maps to exactly one class and nothing raises. An unrecognized
    marker is ``INCONCLUSIVE`` and never silently credited or penalized, so teaching the
    vocabulary a new marker is a deliberate act.
    """
    if not marker:
        return OutcomeClass.INCONCLUSIVE
    if marker in credit_markers():
        return OutcomeClass.CREDIT
    if marker in HARNESS_DEFECT_MARKERS:
        return OutcomeClass.HARNESS_DEFECT
    if marker in ENVIRONMENT_MARKERS:
        return OutcomeClass.ENVIRONMENT
    return OutcomeClass.INCONCLUSIVE


def classify(dispatch: PersonaDispatch) -> OutcomeClass:
    """Classify one dispatch by its ``terminal_marker``.

    Deliberately does not look at ``dispatch.status``: that field collapses negative
    verdicts and harness defects into the same ``"failed"`` value (see the module
    docstring). Reading it would invert the meaning of a correct review.
    """
    return classify_marker(dispatch.terminal_marker)
