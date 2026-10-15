"""#101: the solo-author approval trap, detected and explained. Merge policy is not changed.

A repo whose target branch requires an approving review (classic branch protection, a
ruleset, or code-owner review) cannot be merged by a PR that its own author opened: GitHub
never lets an author approve their own PR. coordinare's identity opens its PRs, so on such
a repo the closer waits for ever.

This module reads the branch's review rules at symphony startup and then about every
``RECHECK_SECONDS``, compares them with coordinare's own identity, and records one
symphony-level condition. A change of condition is logged to the activity feed and, when
the trap is found, sent once through the notification layer. The per-PR closer names the
same wait ``external_review_required`` so a card shows why it is not merging.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import structlog

from coordinare.models.notification import EventType, NotificationEvent, NotificationSeverity

logger = structlog.get_logger(__name__)

GATE_EXTERNAL = "external_review_required"
GATE_CLEAR = "clear"
GATE_UNREADABLE = "unreadable"

# The two sources read for a branch. Both are read every time, so a repo that uses only one
# of them is not mistaken for a repo with neither.
PROTECTION_CLASSIC = "classic"
PROTECTION_RULES = "rules"
PROTECTION_SOURCES = (PROTECTION_CLASSIC, PROTECTION_RULES)

# The branch read when a symphony names none. Matches the daemon's own default base branch.
DEFAULT_TARGET_BRANCH = "main"

# Protection changes rarely. A read every cycle would spend the GitHub budget for nothing.
RECHECK_SECONDS = 900.0

_HTTP_OK = 200
_HTTP_NOT_FOUND = 404


@dataclass(frozen=True)
class ReviewRequirement:
    """What a branch demands before a PR may merge, as far as coordinare can see it."""

    approving_reviews: int = 0
    code_owner_review: bool = False
    last_push_approval: bool = False

    @property
    def requires_review(self) -> bool:
        """True when some account other than the PR's author must approve or be a code owner."""
        return self.approving_reviews > 0 or self.code_owner_review

    def combined(self, other: ReviewRequirement) -> ReviewRequirement:
        """The stricter of two requirements: the larger count, and each flag if either sets it."""
        return ReviewRequirement(
            approving_reviews=max(self.approving_reviews, other.approving_reviews),
            code_owner_review=self.code_owner_review or other.code_owner_review,
            last_push_approval=self.last_push_approval or other.last_push_approval,
        )


@dataclass(frozen=True)
class ProtectionReading:
    """The branch's combined requirement, and the sources that could not be read."""

    requirement: ReviewRequirement
    unreadable: tuple[str, ...]


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def parse_classic_protection(payload: object) -> ReviewRequirement:
    """Classic branch protection (``GET .../branches/{b}/protection``). No review block means none required."""
    if not isinstance(payload, Mapping):
        return ReviewRequirement()
    reviews = payload.get("required_pull_request_reviews")
    if not isinstance(reviews, Mapping):
        return ReviewRequirement()
    return ReviewRequirement(
        approving_reviews=_count(reviews.get("required_approving_review_count")),
        code_owner_review=reviews.get("require_code_owner_reviews") is True,
        last_push_approval=reviews.get("require_last_push_approval") is True,
    )


def parse_rules(payload: object) -> ReviewRequirement:
    """Rulesets (``GET .../rules/branches/{b}``): the ``pull_request`` rules of every active ruleset.

    Raises ValueError when the payload is not a list of rules, so the caller reports the source
    as unreadable rather than as clear.
    """
    if not isinstance(payload, list):
        msg = "ruleset payload is not a list"
        raise ValueError(msg)
    requirement = ReviewRequirement()
    for rule in payload:
        if not isinstance(rule, Mapping) or rule.get("type") != "pull_request":
            continue
        parameters = rule.get("parameters")
        if not isinstance(parameters, Mapping):
            continue
        requirement = requirement.combined(ReviewRequirement(
            approving_reviews=_count(parameters.get("required_approving_review_count")),
            code_owner_review=parameters.get("require_code_owner_review") is True,
            last_push_approval=parameters.get("require_last_push_approval") is True,
        ))
    return requirement


def read_protection(sources: Mapping[str, tuple[int, object]]) -> ProtectionReading:
    """Combine the per-source (HTTP status, JSON) pairs into one reading.

    A 404 on the classic endpoint means the branch has no classic protection, which is a
    readable answer. Any other non-200 status, including 0 (no answer at all), makes that source
    unreadable, and an unreadable source is never read as "no requirement".
    """
    requirement = ReviewRequirement()
    unreadable: list[str] = []
    for name in PROTECTION_SOURCES:
        status, payload = sources.get(name, (0, None))
        if status == _HTTP_NOT_FOUND and name == PROTECTION_CLASSIC:
            continue
        if status != _HTTP_OK:
            unreadable.append(name)
            continue
        try:
            parsed = parse_classic_protection(payload) if name == PROTECTION_CLASSIC else parse_rules(payload)
        except ValueError:
            unreadable.append(name)
            continue
        requirement = requirement.combined(parsed)
    return ProtectionReading(requirement=requirement, unreadable=tuple(unreadable))


def _display_identity(logins: frozenset[str]) -> str:
    """The name the operator knows coordinare by: the App slug, not its ``[bot]`` login."""
    names = sorted(login for login in logins if not login.endswith("[bot]"))
    return names[0] if names else sorted(logins)[0]


def _unreadable_message(branch: str) -> str:
    return (
        f"could not read branch protection on {branch}, so coordinare cannot tell whether merges need "
        "a review it cannot give. Check the token's repository permissions."
    )


def judge(reading: ProtectionReading, daemon_logins: frozenset[str], branch: str) -> tuple[str, str]:
    """The symphony's condition and the sentence that explains it to an operator.

    The daemon opens its PRs, so a requirement for an approving review (or a code owner's) is a
    trap whenever coordinare's identity is known. The identity comparison is the author check:
    an author cannot approve their own PR.
    """
    if reading.requirement.requires_review and daemon_logins:
        return GATE_EXTERNAL, (
            f"Merges need an approving review from someone other than {_display_identity(daemon_logins)}. "
            "Add a human approver to the loop or relax the rule."
        )
    if reading.requirement.requires_review:
        return GATE_UNREADABLE, (
            f"A review is required on {branch}, but coordinare could not resolve its own GitHub identity, "
            "so the requirement was not compared."
        )
    if reading.unreadable:
        return GATE_UNREADABLE, _unreadable_message(branch)
    return GATE_CLEAR, f"{branch} needs no approving review from another account."


def pr_waits_on_external_review(
    *, review_decision: str, merge_state_status: str, author_login: str, daemon_logins: frozenset[str],
) -> bool:
    """The closer's per-PR test: GitHub says a review is required, the PR is blocked, and coordinare opened it."""
    author = author_login.strip().lower()
    return (
        review_decision.upper() == "REVIEW_REQUIRED"
        and merge_state_status.upper() == "BLOCKED"
        and bool(author)
        and author in daemon_logins
    )


@dataclass(frozen=True)
class ReviewProbe:
    """Where one symphony's review rules are read, and where a change of condition is reported."""

    symphony: str
    branch: str
    github: Any
    notification_service: Any
    activity: Any


async def refresh_merge_review(sym_state: Any, probe: ReviewProbe, *, now: float) -> None:
    """Re-read the branch's review rules if the last read is older than ``RECHECK_SECONDS``.

    Records the condition on ``sym_state.merge_review``. Only a change of condition is reported:
    one activity entry, and for the trap one warning through the notification layer. Never raises,
    and never blocks dispatch: any failure is recorded as unreadable.
    """
    checked_at = getattr(sym_state, "merge_review_checked_at", None)
    if isinstance(checked_at, float) and now - checked_at < RECHECK_SECONDS:
        return
    sym_state.merge_review_checked_at = now
    try:
        reading = read_protection(await probe.github.get_branch_protection_sources(probe.branch))
        state, message = judge(reading, await probe.github.self_logins(), probe.branch)
    except Exception as exc:
        logger.warning("merge_review_gate.read_failed", symphony=probe.symphony, branch=probe.branch, error=str(exc))
        state, message = GATE_UNREADABLE, _unreadable_message(probe.branch)

    previous = getattr(sym_state, "merge_review", None)
    previous_state = previous.get("state") if isinstance(previous, dict) else GATE_CLEAR
    sym_state.merge_review = {"state": state, "message": message, "branch": probe.branch}
    if state == previous_state:
        return
    logger.info("merge_review_gate.changed", symphony=probe.symphony, branch=probe.branch, state=state)
    if probe.activity is not None:
        probe.activity.record(activity_type="merge_review_gate", text=message, status=state)
    if state == GATE_EXTERNAL and probe.notification_service is not None:
        try:
            await probe.notification_service.dispatch(NotificationEvent(
                event_type=EventType.merge_review_required,
                severity=NotificationSeverity.warning,
                source="merge_review_gate",
                payload={"symphony": probe.symphony, "branch": probe.branch, "message": message},
                dedup_key=f"merge_review_required:{probe.symphony}",
            ))
        except Exception as exc:
            logger.warning("merge_review_gate.notify_failed", symphony=probe.symphony, error=str(exc))


def merge_review_view(sym_state: Any) -> dict[str, str] | None:
    """The condition as the dashboard reads it, or None before the first read."""
    review = getattr(sym_state, "merge_review", None) if sym_state is not None else None
    if not isinstance(review, dict):
        return None
    return {"state": str(review.get("state", "")), "message": str(review.get("message", ""))}
