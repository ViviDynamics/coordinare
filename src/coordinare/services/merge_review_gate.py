"""#101: the solo-author approval trap, detected and explained. Merge policy is not changed.

A repo whose target branch requires an approving review (classic branch protection, a
ruleset, a code owner's review, or a last-push approval) cannot be merged by a PR that its own
author opened: GitHub never lets an author approve their own PR. coordinare's identity opens
its PRs, so on such a repo the closer waits for ever.

This module reads the branch's review rules at symphony startup and then about every
``RECHECK_SECONDS``, compares them with coordinare's own identity, and records one
symphony-level condition. A ruleset that is not enforced (evaluate or disabled mode), or that
lets the daemon's App bypass it, does not count. A change of condition is logged to the
activity feed and, when the trap is found, sent once through the notification layer. The
per-PR closer names the same wait ``external_review_required`` so a card shows why it is not
merging.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import structlog

from coordinare.models.notification import EventType, NotificationEvent, NotificationSeverity

logger = structlog.get_logger(__name__)

GATE_EXTERNAL = "external_review_required"
GATE_CLEAR = "clear"
GATE_UNREADABLE = "unreadable"
# The App is a bypass actor for the review rule, so the requirement does not hold for it. It is
# not a warning: no review is needed, and the per-PR closer never claims one.
GATE_BYPASSED = "bypassed"

# The two sources read for a branch. Both are read every time, so a repo that uses only one
# of them is not mistaken for a repo with neither. Each ruleset that the rules list names is
# also read by its own key (``ruleset:<id>``), for its enforcement mode and bypass list.
PROTECTION_CLASSIC = "classic"
PROTECTION_RULES = "rules"
PROTECTION_SOURCES = (PROTECTION_CLASSIC, PROTECTION_RULES)
RULESET_SOURCE_PREFIX = "ruleset:"

# Protection changes rarely. A read every cycle would spend the GitHub budget for nothing.
RECHECK_SECONDS = 900.0

# The branch the gate reads. The daemon has no per-symphony base branch: rebase and the CI gate
# assume ``main`` in the same way, so this matches what the rest of the closer does.
DEFAULT_TARGET_BRANCH = "main"

# Check states that are not settled. A BLOCKED PR whose checks failed or are still running is
# waiting on CI, not on a review, so it is never tagged external_review_required.
UNSETTLED_CHECK_STATES = frozenset({"FAILURE", "ERROR", "PENDING", "EXPECTED"})

# A bypass mode that lets the actor merge a PR past the pull_request rule.
_BYPASS_MODES = frozenset({"always", "pull_request"})
_RULESET_ACTIVE = "active"
_INTEGRATION_ACTOR = "Integration"

_HTTP_OK = 200
_HTTP_NOT_FOUND = 404


@dataclass(frozen=True)
class ReviewRequirement:
    """What a branch demands before a PR may merge, as far as coordinare can see it."""

    approving_reviews: int = 0
    code_owner_review: bool = False
    last_push_approval: bool = False
    dismiss_stale_reviews_on_push: bool = False
    require_extra_approval_for_unattributed_changes: bool = False

    @property
    def requires_review(self) -> bool:
        """True when a PR needs something its own author cannot give.

        An approving review, a code owner's review, or an approval of the last push each needs
        an approval from someone else. A ruleset can set only the last one (count 0), and GitHub
        still blocks the author's PR on it.
        """
        return (
            self.approving_reviews > 0 or self.code_owner_review or self.last_push_approval
            or self.require_extra_approval_for_unattributed_changes
        )

    def combined(self, other: ReviewRequirement) -> ReviewRequirement:
        """The stricter of two requirements: the larger count, and each flag if either sets it."""
        return ReviewRequirement(
            approving_reviews=max(self.approving_reviews, other.approving_reviews),
            code_owner_review=self.code_owner_review or other.code_owner_review,
            last_push_approval=self.last_push_approval or other.last_push_approval,
            dismiss_stale_reviews_on_push=self.dismiss_stale_reviews_on_push or other.dismiss_stale_reviews_on_push,
            require_extra_approval_for_unattributed_changes=(
                self.require_extra_approval_for_unattributed_changes or other.require_extra_approval_for_unattributed_changes
            ),
        )


@dataclass(frozen=True)
class RuleScope:
    """The pull_request rules that apply to merges, and what was decided about each ruleset."""

    kept: list[Any]
    unverified: tuple[str, ...]
    inactive: tuple[str, ...]
    bypassed: tuple[str, ...]
    bypassed_rules: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class ProtectionReading:
    """The branch's combined requirement, the sources that could not be read, and the rulesets
    that were set aside (inactive or bypassed) or kept without being verified.

    ``bypass_cleared`` is True when the App is a bypass actor for a requirement that would
    otherwise hold, so the requirement is gone for coordinare's merges.
    """

    requirement: ReviewRequirement
    unreadable: tuple[str, ...]
    unverified: tuple[str, ...] = ()
    inactive: tuple[str, ...] = ()
    bypassed: tuple[str, ...] = ()
    bypass_cleared: bool = False
    # A merge allowance does not undo review dismissal caused by a push.
    push_requirement: ReviewRequirement = field(default_factory=ReviewRequirement)


@dataclass(frozen=True)
class PrFacts:
    """The fields GitHub gives for one PR that the closer's wait test reads."""

    review_decision: str
    merge_state_status: str
    author_login: str
    checks_state: str


def ruleset_source_key(ruleset_id: str) -> str:
    return f"{RULESET_SOURCE_PREFIX}{ruleset_id}"


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
        dismiss_stale_reviews_on_push=reviews.get("dismiss_stale_reviews") is True,
        require_extra_approval_for_unattributed_changes=reviews.get("require_extra_approval_for_unattributed_changes") is True,
    )


def parse_rules(rules: list[Any]) -> ReviewRequirement:
    """The requirement of the ``pull_request`` rules in a rules list."""
    requirement = ReviewRequirement()
    for rule in rules:
        if not isinstance(rule, Mapping) or rule.get("type") != "pull_request":
            continue
        parameters = rule.get("parameters")
        if not isinstance(parameters, Mapping):
            continue
        requirement = requirement.combined(ReviewRequirement(
            approving_reviews=_count(parameters.get("required_approving_review_count")),
            code_owner_review=parameters.get("require_code_owner_review") is True,
            last_push_approval=parameters.get("require_last_push_approval") is True,
            dismiss_stale_reviews_on_push=parameters.get("dismiss_stale_reviews_on_push") is True,
            require_extra_approval_for_unattributed_changes=parameters.get("require_extra_approval_for_unattributed_changes") is True,
        ))
    return requirement


def ruleset_ids(payload: object) -> list[str]:
    """The distinct ruleset ids named by the ``pull_request`` rules of a rules payload, in order."""
    if not isinstance(payload, list):
        return []
    ids: dict[str, None] = {}
    for rule in payload:
        if isinstance(rule, Mapping) and rule.get("type") == "pull_request" and rule.get("ruleset_id") is not None:
            ids[str(rule["ruleset_id"])] = None
    return list(ids)


def parse_ruleset_detail(payload: object, daemon_app_id: int | None) -> tuple[str, bool] | None:
    """A ruleset's enforcement and whether the daemon's App is a bypass actor for it.

    Only an Integration actor with the App's own id counts as the daemon. Organisation admins and
    teams may include coordinare, but the API does not say, so they are not counted. Returns None
    when the payload is not a ruleset object.
    """
    if not isinstance(payload, Mapping):
        return None
    enforcement = str(payload.get("enforcement") or "")
    actors = payload.get("bypass_actors")
    bypass = daemon_app_id is not None and isinstance(actors, list) and any(
        isinstance(actor, Mapping)
        and actor.get("actor_type") == _INTEGRATION_ACTOR
        and actor.get("actor_id") == daemon_app_id
        and actor.get("bypass_mode") in _BYPASS_MODES
        for actor in actors
    )
    return enforcement, bypass


def scope_rules(
    payload: object, details: Mapping[str, tuple[int, object]], daemon_app_id: int | None,
) -> RuleScope:
    """Keep the ``pull_request`` rules that apply to a merge by the daemon.

    A rule whose ruleset is not ``active`` is set aside (evaluate mode does not block a merge),
    and so is one the daemon's App bypasses. A ruleset whose detail could not be read is kept:
    the conservative reading, and it is named in ``unverified``. Raises ValueError when the rules
    payload is not a list.
    """
    if not isinstance(payload, list):
        msg = "ruleset payload is not a list"
        raise ValueError(msg)
    kept: list[Any] = []
    unverified: dict[str, None] = {}
    inactive: dict[str, None] = {}
    bypassed: dict[str, None] = {}
    bypassed_rules: list[Any] = []
    for rule in payload:
        if not isinstance(rule, Mapping) or rule.get("type") != "pull_request":
            continue
        if rule.get("ruleset_id") is None:
            kept.append(rule)
            continue
        ruleset = str(rule["ruleset_id"])
        status, detail = details.get(ruleset_source_key(ruleset), (0, None))
        if status != _HTTP_OK:
            kept.append(rule)
            unverified[ruleset] = None
            continue
        parsed = parse_ruleset_detail(detail, daemon_app_id)
        if parsed is None:
            kept.append(rule)
            unverified[ruleset] = None
            continue
        enforcement, bypass = parsed
        if enforcement != _RULESET_ACTIVE:
            inactive[ruleset] = None
        elif bypass:
            bypassed[ruleset] = None
            bypassed_rules.append(rule)
        else:
            kept.append(rule)
    return RuleScope(
        kept=kept,
        unverified=tuple(unverified),
        inactive=tuple(inactive),
        bypassed=tuple(bypassed),
        bypassed_rules=bypassed_rules,
    )


def classic_bypass_lists_app(payload: object, daemon_app_id: int | None) -> bool:
    """True when the classic protection lists coordinare's App in its pull request bypass allowances."""
    if daemon_app_id is None or not isinstance(payload, Mapping):
        return False
    reviews = payload.get("required_pull_request_reviews")
    allowances = reviews.get("bypass_pull_request_allowances") if isinstance(reviews, Mapping) else None
    apps = allowances.get("apps") if isinstance(allowances, Mapping) else None
    return isinstance(apps, list) and any(
        isinstance(app, Mapping) and app.get("id") == daemon_app_id for app in apps
    )


def read_protection(
    sources: Mapping[str, tuple[int, object]], daemon_app_id: int | None = None,
) -> ProtectionReading:
    """Combine the per-source (HTTP status, JSON) pairs into one reading.

    A 404 on the classic endpoint means the branch has no classic protection, which is a
    readable answer. Any other non-200 status, including 0 (no answer at all), makes that source
    unreadable, and an unreadable source is never read as "no requirement".
    """
    requirement = ReviewRequirement()
    push_requirement = ReviewRequirement()
    unreadable: list[str] = []
    bypass_cleared = False
    scope = RuleScope(kept=[], unverified=(), inactive=(), bypassed=())
    for name in PROTECTION_SOURCES:
        status, payload = sources.get(name, (0, None))
        if status == _HTTP_NOT_FOUND and name == PROTECTION_CLASSIC:
            continue
        if status != _HTTP_OK:
            unreadable.append(name)
            continue
        try:
            if name == PROTECTION_CLASSIC:
                parsed = parse_classic_protection(payload)
                push_requirement = push_requirement.combined(parsed)
                if parsed.requires_review and classic_bypass_lists_app(payload, daemon_app_id):
                    bypass_cleared = True
                    parsed = ReviewRequirement()
            else:
                scope = scope_rules(payload, sources, daemon_app_id)
                parsed = parse_rules(scope.kept)
                push_requirement = push_requirement.combined(parse_rules(scope.kept + scope.bypassed_rules))
                bypass_cleared = bypass_cleared or parse_rules(scope.bypassed_rules).requires_review
        except ValueError:
            unreadable.append(name)
            continue
        requirement = requirement.combined(parsed)
    return ProtectionReading(
        requirement=requirement,
        unreadable=tuple(unreadable),
        unverified=scope.unverified,
        inactive=scope.inactive,
        bypassed=scope.bypassed,
        bypass_cleared=bypass_cleared,
        push_requirement=push_requirement,
    )


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

    The daemon opens its PRs, so a requirement for a review it cannot give is a trap whenever
    coordinare's identity is known. The identity comparison is the author check: an author cannot
    approve their own PR.
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
    if reading.bypass_cleared:
        return GATE_BYPASSED, (
            f"{branch} needs an approving review, but coordinare's App is a bypass actor for it, "
            "so merges are not held for one."
        )
    return GATE_CLEAR, f"{branch} needs no approving review from another account."


def pr_waits_on_external_review(
    facts: PrFacts, *, condition: str | None, daemon_logins: frozenset[str],
) -> bool:
    """The closer's per-PR test for a wait that no one inside the loop can clear.

    The PR must be BLOCKED, its checks must be settled (neither failing nor still running, since
    then the wait is CI), and it must have been opened by coordinare's identity. Then it is an
    external review wait when GitHub says a review is required, or when the symphony-level
    condition says the branch needs a review the daemon cannot give. An empty ``reviewDecision``
    is common on these PRs, so the condition is what carries the tag. A bypassed condition never
    tags, whatever GitHub's ``reviewDecision`` says.
    """
    if condition == GATE_BYPASSED:
        return False
    if facts.merge_state_status.upper() != "BLOCKED":
        return False
    if facts.checks_state.upper() in UNSETTLED_CHECK_STATES:
        return False
    author = facts.author_login.strip().lower()
    if not author or author not in daemon_logins:
        return False
    if facts.review_decision.upper() == "REVIEW_REQUIRED":
        return True
    return condition == GATE_EXTERNAL


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
    unverified: tuple[str, ...] = ()
    sym_state.merge_review_rules = {}
    try:
        app_id = await probe.github.daemon_app_id()
        reading = read_protection(
            await probe.github.get_branch_protection_sources(probe.branch), app_id,
        )
        unverified = reading.unverified
        state, message = judge(reading, await probe.github.self_logins(), probe.branch)
        if not reading.unreadable and not unverified:
            sym_state.merge_review_rules = {
                "dismiss_stale_reviews_on_push": reading.push_requirement.dismiss_stale_reviews_on_push,
                "require_extra_approval_for_unattributed_changes": reading.push_requirement.require_extra_approval_for_unattributed_changes,
            }
    except Exception as exc:
        logger.warning("merge_review_gate.read_failed", symphony=probe.symphony, branch=probe.branch, error=str(exc))
        state, message = GATE_UNREADABLE, _unreadable_message(probe.branch)

    previous = getattr(sym_state, "merge_review", None)
    previous_state = previous.get("state") if isinstance(previous, dict) else GATE_CLEAR
    sym_state.merge_review = {"state": state, "message": message, "branch": probe.branch}
    if state == previous_state:
        return
    logger.info(
        "merge_review_gate.changed", symphony=probe.symphony, branch=probe.branch, state=state,
        unverified_rulesets=list(unverified),
    )
    if probe.activity is not None:
        named = f" Rulesets kept without being verified: {', '.join(unverified)}." if unverified else ""
        probe.activity.record(activity_type="merge_review_gate", text=message + named, status=state)
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
