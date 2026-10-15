"""#101: the solo-author approval trap is detected and explained, not merged around.

On a repo whose target branch requires an approving review, a PR that coordinare's own
identity authored can never be approved by its author, so the closer waits for ever. The
symphony-level gate reads the branch's review rules (classic protection and rulesets),
compares them with the daemon's identity, and surfaces the condition once per change. The
per-PR closer names the wait ``external_review_required``. Merge policy is unchanged.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest
import structlog.testing

from coordinare.graph.nodes import monitor_pr as monitor_pr_module
from coordinare.graph.nodes.monitor_pr import monitor_pr
from coordinare.graph.state import SymphonyRuntimeState, initial_state
from coordinare.services import merge_review_gate as gate
from coordinare.services.activity_log import ActivityLog
from coordinare.services.github import GitHubService

_APP = frozenset({"vivi-coordinare", "vivi-coordinare[bot]"})
_OK = 200
_NOT_FOUND = 404
_FORBIDDEN = 403
_CLASSIC = "classic"
_RULES = "rules"


def _classic(count: int = 1, *, code_owner: bool = False, last_push: bool = False) -> tuple[int, object]:
    return _OK, {"required_pull_request_reviews": {
        "required_approving_review_count": count,
        "require_code_owner_reviews": code_owner,
        "require_last_push_approval": last_push,
    }}


def _rules(count: int = 1, *, code_owner: bool = False, last_push: bool = False) -> tuple[int, object]:
    return _OK, [{"type": "pull_request", "parameters": {
        "required_approving_review_count": count,
        "require_code_owner_review": code_owner,
        "require_last_push_approval": last_push,
    }}]


def _sources(classic: tuple[int, object], rules: tuple[int, object]) -> dict[str, tuple[int, object]]:
    return {_CLASSIC: classic, _RULES: rules}


# --- parsing: classic protection and rulesets ---------------------------------- #


def test_classic_protection_with_approving_reviews_requires_a_review() -> None:
    requirement = gate.parse_classic_protection(_classic(2)[1])
    assert requirement == gate.ReviewRequirement(approving_reviews=2)
    assert requirement.requires_review


def test_classic_protection_without_pull_request_reviews_requires_nothing() -> None:
    requirement = gate.parse_classic_protection({"required_status_checks": {"strict": True}})
    assert requirement == gate.ReviewRequirement()
    assert not requirement.requires_review


def test_classic_code_owner_and_last_push_flags_are_read() -> None:
    requirement = gate.parse_classic_protection(_classic(0, code_owner=True, last_push=True)[1])
    assert requirement.code_owner_review and requirement.last_push_approval
    assert requirement.requires_review  # a code-owner review is a review someone else must give


def test_classic_non_integer_count_is_treated_as_no_requirement() -> None:
    payload = {"required_pull_request_reviews": {"required_approving_review_count": "two"}}
    assert gate.parse_classic_protection(payload) == gate.ReviewRequirement()


def test_ruleset_pull_request_rule_counts_its_approvals() -> None:
    requirement = gate.parse_rules(_rules(1, last_push=True)[1])
    assert requirement.approving_reviews == 1 and requirement.last_push_approval
    assert requirement.requires_review


def test_ruleset_without_a_pull_request_rule_requires_nothing() -> None:
    rules = [{"type": "deletion", "parameters": {}}, {"type": "non_fast_forward"}]
    assert gate.parse_rules(rules) == gate.ReviewRequirement()


def test_ruleset_payload_that_is_not_a_list_is_rejected() -> None:
    with pytest.raises(ValueError, match="not a list"):
        gate.parse_rules({"message": "unexpected"})


# --- reading the two sources together ------------------------------------------- #


def test_classic_only_requirement_is_read() -> None:
    reading = gate.read_protection(_sources(_classic(1), (_OK, [])))
    assert reading.requirement.requires_review
    assert reading.unreadable == ()


def test_rules_only_requirement_is_read() -> None:
    reading = gate.read_protection(_sources((_NOT_FOUND, None), _rules(1)))
    assert reading.requirement.requires_review
    assert reading.unreadable == ()


def test_both_sources_take_the_strictest_count() -> None:
    reading = gate.read_protection(_sources(_classic(1), _rules(2)))
    assert reading.requirement.approving_reviews == 2


def test_neither_source_requires_nothing_and_both_were_readable() -> None:
    reading = gate.read_protection(_sources((_NOT_FOUND, None), (_OK, [])))
    assert not reading.requirement.requires_review
    assert reading.unreadable == ()


def test_unreadable_in_both_sources_is_reported_by_name() -> None:
    reading = gate.read_protection(_sources((_FORBIDDEN, None), (_FORBIDDEN, None)))
    assert not reading.requirement.requires_review
    assert reading.unreadable == (_CLASSIC, _RULES)


def test_one_unreadable_source_does_not_hide_a_requirement_in_the_other() -> None:
    reading = gate.read_protection(_sources((_FORBIDDEN, None), _rules(1)))
    assert reading.requirement.requires_review
    assert reading.unreadable == (_CLASSIC,)


def test_one_unreadable_source_means_no_clear_verdict() -> None:
    reading = gate.read_protection(_sources((_FORBIDDEN, None), (_OK, [])))
    assert not reading.requirement.requires_review
    assert reading.unreadable == (_CLASSIC,)


def test_malformed_ruleset_payload_counts_as_unreadable() -> None:
    reading = gate.read_protection(_sources((_NOT_FOUND, None), (_OK, {"oops": True})))
    assert reading.unreadable == (_RULES,)


# --- the author-identity comparison --------------------------------------------- #


def test_requirement_with_daemon_identity_is_external() -> None:
    reading = gate.read_protection(_sources(_classic(1), (_OK, [])))
    state, message = gate.judge(reading, _APP, "main")
    assert state == gate.GATE_EXTERNAL
    assert message == (
        "Merges need an approving review from someone other than vivi-coordinare. "
        "Add a human approver to the loop or relax the rule."
    )


def test_bot_suffix_is_not_the_name_shown_to_the_operator() -> None:
    logins = frozenset({"vivi-coordinare[bot]", "vivi-coordinare"})
    state, message = gate.judge(gate.read_protection(_sources(_classic(1), (_OK, []))), logins, "main")
    assert state == gate.GATE_EXTERNAL and "from someone other than vivi-coordinare." in message


def test_requirement_without_a_known_identity_is_unreadable_not_external() -> None:
    state, message = gate.judge(gate.read_protection(_sources(_classic(1), (_OK, []))), frozenset(), "main")
    assert state == gate.GATE_UNREADABLE
    assert "could not resolve" in message


def test_no_requirement_is_clear() -> None:
    state, _ = gate.judge(gate.read_protection(_sources((_NOT_FOUND, None), (_OK, []))), _APP, "main")
    assert state == gate.GATE_CLEAR


def test_unreadable_protection_says_so_in_plain_words() -> None:
    state, message = gate.judge(gate.read_protection(_sources((_FORBIDDEN, None), (_FORBIDDEN, None))), _APP, "main")
    assert state == gate.GATE_UNREADABLE
    assert "could not read branch protection" in message


def test_a_known_requirement_wins_over_an_unreadable_source() -> None:
    reading = gate.read_protection(_sources((_FORBIDDEN, None), _rules(1)))
    state, _ = gate.judge(reading, _APP, "main")
    assert state == gate.GATE_EXTERNAL


# --- the per-PR wait reason ----------------------------------------------------- #


@pytest.mark.parametrize(
    ("decision", "merge_state", "author", "expected"),
    [
        ("REVIEW_REQUIRED", "BLOCKED", "vivi-coordinare", True),
        ("REVIEW_REQUIRED", "BLOCKED", "Vivi-Coordinare[bot]", True),
        ("REVIEW_REQUIRED", "BLOCKED", "jason", False),
        ("REVIEW_REQUIRED", "CLEAN", "vivi-coordinare", False),
        ("APPROVED", "BLOCKED", "vivi-coordinare", False),
        ("", "BLOCKED", "vivi-coordinare", False),
        ("REVIEW_REQUIRED", "BLOCKED", "", False),
    ],
)
def test_pr_waits_on_external_review_only_for_the_daemon_authored_blocked_pr(
    decision: str, merge_state: str, author: str, expected: bool,
) -> None:
    assert gate.pr_waits_on_external_review(
        review_decision=decision, merge_state_status=merge_state,
        author_login=author, daemon_logins=_APP,
    ) is expected


# --- refresh: startup, cadence and dedupe across cycles ------------------------- #


class _Notifier:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def dispatch(self, event: Any) -> None:
        self.events.append(event)


class _Github:
    """Stands in for GitHubService: protection sources and the daemon identity."""

    def __init__(self, classic: tuple[int, object], rules: tuple[int, object], logins: frozenset[str] = _APP) -> None:
        self.sources = _sources(classic, rules)
        self.logins = logins
        self.reads = 0

    async def get_branch_protection_sources(self, branch: str) -> dict[str, tuple[int, object]]:
        self.reads += 1
        return self.sources

    async def self_logins(self) -> frozenset[str]:
        return self.logins


class _BrokenGithub:
    async def get_branch_protection_sources(self, branch: str) -> dict[str, tuple[int, object]]:
        msg = "boom"
        raise RuntimeError(msg)

    async def self_logins(self) -> frozenset[str]:
        return _APP


async def _refresh(sym: SymphonyRuntimeState, github: Any, notifier: _Notifier, log: ActivityLog, now: float) -> None:
    probe = gate.ReviewProbe(
        symphony="garden", branch="main", github=github, notification_service=notifier, activity=log,
    )
    await gate.refresh_merge_review(sym, probe, now=now)


@pytest.mark.asyncio
async def test_startup_surfaces_the_trap_once_with_a_warning_and_an_activity_entry() -> None:
    sym = SymphonyRuntimeState(name="garden")
    github, notifier, log = _Github(_classic(1), (_OK, [])), _Notifier(), ActivityLog()
    await _refresh(sym, github, notifier, log, now=1000.0)
    assert sym.merge_review is not None and sym.merge_review["state"] == gate.GATE_EXTERNAL
    (event,) = notifier.events
    assert event.event_type.value == "merge_review_required"
    assert event.severity.value == "warning"
    assert event.dedup_key == "merge_review_required:garden"
    assert event.payload["symphony"] == "garden" and event.payload["branch"] == "main"
    assert [entry["text"] for entry in log.snapshot()] == [sym.merge_review["message"]]


@pytest.mark.asyncio
async def test_reads_are_not_repeated_within_the_recheck_window() -> None:
    sym = SymphonyRuntimeState(name="garden")
    github, notifier, log = _Github(_classic(1), (_OK, [])), _Notifier(), ActivityLog()
    await _refresh(sym, github, notifier, log, now=1000.0)
    await _refresh(sym, github, notifier, log, now=1000.0 + gate.RECHECK_SECONDS - 1)
    assert github.reads == 1


@pytest.mark.asyncio
async def test_an_unchanged_condition_is_not_reported_again_after_the_window() -> None:
    sym = SymphonyRuntimeState(name="garden")
    github, notifier, log = _Github(_classic(1), (_OK, [])), _Notifier(), ActivityLog()
    await _refresh(sym, github, notifier, log, now=1000.0)
    await _refresh(sym, github, notifier, log, now=1000.0 + gate.RECHECK_SECONDS)
    assert github.reads == 2
    assert len(notifier.events) == 1
    assert len(log.snapshot()) == 1


@pytest.mark.asyncio
async def test_clearing_the_condition_is_logged_but_not_paged() -> None:
    sym = SymphonyRuntimeState(name="garden")
    github, notifier, log = _Github(_classic(1), (_OK, [])), _Notifier(), ActivityLog()
    await _refresh(sym, github, notifier, log, now=1000.0)
    github.sources = _sources((_NOT_FOUND, None), (_OK, []))
    await _refresh(sym, github, notifier, log, now=1000.0 + gate.RECHECK_SECONDS)
    assert sym.merge_review is not None and sym.merge_review["state"] == gate.GATE_CLEAR
    assert len(notifier.events) == 1
    assert len(log.snapshot()) == 2


@pytest.mark.asyncio
async def test_a_clear_repository_is_silent() -> None:
    sym = SymphonyRuntimeState(name="garden")
    github, notifier, log = _Github((_NOT_FOUND, None), (_OK, [])), _Notifier(), ActivityLog()
    await _refresh(sym, github, notifier, log, now=1000.0)
    assert notifier.events == [] and log.snapshot() == []


@pytest.mark.asyncio
async def test_a_failed_read_degrades_to_unreadable_and_never_raises() -> None:
    sym = SymphonyRuntimeState(name="garden")
    notifier, log = _Notifier(), ActivityLog()
    await _refresh(sym, _BrokenGithub(), notifier, log, now=1000.0)
    assert sym.merge_review is not None and sym.merge_review["state"] == gate.GATE_UNREADABLE
    assert "could not read branch protection" in sym.merge_review["message"]
    assert notifier.events == []


@pytest.mark.asyncio
async def test_missing_notifier_and_activity_log_still_record_the_condition() -> None:
    sym = SymphonyRuntimeState(name="garden")
    probe = gate.ReviewProbe(
        symphony="garden", branch="main", github=_Github(_classic(1), (_OK, [])),
        notification_service=None, activity=None,
    )
    await gate.refresh_merge_review(sym, probe, now=1000.0)
    assert sym.merge_review is not None and sym.merge_review["state"] == gate.GATE_EXTERNAL


# --- the REST read itself ------------------------------------------------------- #


def _service(**kwargs: Any) -> GitHubService:
    return GitHubService(org="ViviDynamics", project_number=1, token="t", retry_kwargs={"attempts": 1}, **kwargs)


@pytest.mark.asyncio
async def test_rest_read_asks_for_both_sources_and_keeps_their_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/protection"):
            return httpx.Response(_OK, json={"required_pull_request_reviews": {"required_approving_review_count": 1}})
        return httpx.Response(_FORBIDDEN, json={"message": "Resource not accessible"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "coordinare.services.github.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    service = _service()
    service._project_name = "garden"
    sources = await service.get_branch_protection_sources("main")
    assert sources[_CLASSIC][0] == _OK and sources[_RULES][0] == _FORBIDDEN
    assert seen == ["/repos/ViviDynamics/garden/branches/main/protection", "/repos/ViviDynamics/garden/rules/branches/main"]
    assert gate.read_protection(sources).unreadable == (_RULES,)


@pytest.mark.asyncio
async def test_rest_read_without_a_repository_name_reports_both_sources_unreadable() -> None:
    sources = await _service().get_branch_protection_sources("main")
    assert sources == {_CLASSIC: (0, None), _RULES: (0, None)}


@pytest.mark.asyncio
async def test_rest_read_of_a_server_error_gives_up_as_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(
        "coordinare.services.github.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    service = _service()
    service._project_name = "garden"
    sources = await service.get_branch_protection_sources("main")
    assert sources == {_CLASSIC: (0, None), _RULES: (0, None)}


# --- monitor_pr: the card's wait reason ------------------------------------------ #


class _PrGithub:
    def __init__(self, *, decision: str, merge_state: str, author: str) -> None:
        self._ctx = {
            "reviews": [], "review_threads": [], "head_oid": "a" * 40, "review_decision": decision,
            "merge_state_status": merge_state, "author_login": author, "state": "OPEN",
        }

    async def get_pr_review_context(self, pr_id: str) -> dict[str, Any]:
        return self._ctx

    async def self_logins(self) -> frozenset[str]:
        return _APP


def _pr_state(github: _PrGithub) -> dict:
    state = initial_state()
    state["current_card"] = {"pr_node_id": "PR_101", "id": "CARD_101", "title": "solo"}
    state["human_reviewers"] = ["jason"]
    state["github_service"] = github
    state["lifecycle_completed_at"] = None
    return state


@pytest.fixture(autouse=True)
def _clean_wait_counters():
    monitor_pr_module._merge_waits.clear()
    yield
    monitor_pr_module._merge_waits.clear()


@pytest.mark.asyncio
async def test_daemon_authored_blocked_pr_is_waiting_on_external_review() -> None:
    state = _pr_state(_PrGithub(decision="REVIEW_REQUIRED", merge_state="BLOCKED", author="vivi-coordinare"))
    with structlog.testing.capture_logs() as logs:
        result = await monitor_pr(state)
    assert result["phase"] == "monitoring_pr"
    assert result["review_wait_reason"] == "external_review_required"
    (decision,) = [e for e in logs if e["event"] == "monitor_pr.merge_decision"]
    assert decision["reason"] == "external_review_required" and decision["outcome"] == "waiting"
    assert decision["merge_state_status"] == "BLOCKED"


@pytest.mark.asyncio
async def test_a_human_authored_pr_keeps_the_generic_approval_wait() -> None:
    state = _pr_state(_PrGithub(decision="REVIEW_REQUIRED", merge_state="BLOCKED", author="jason"))
    result = await monitor_pr(state)
    assert result["review_wait_reason"] == "no_human_approval"


@pytest.mark.asyncio
async def test_a_daemon_pr_that_is_clean_is_not_called_external() -> None:
    state = _pr_state(_PrGithub(decision="REVIEW_REQUIRED", merge_state="CLEAN", author="vivi-coordinare"))
    result = await monitor_pr(state)
    assert result["review_wait_reason"] == "no_human_approval"
