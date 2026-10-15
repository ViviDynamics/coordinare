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
_APP_ID = 3_100_001
_OK = 200
_NOT_FOUND = 404
_FORBIDDEN = 403
_CLASSIC = "classic"
_RULES = "rules"
_ELM_RULESET = "14616733"

# Elm's main ruleset, as GitHub returned it (read-only GET, 2026-10-15). A solo author can never
# satisfy it: no approving review is required, but the last push must be approved.
_ELM_RULES: list[dict[str, Any]] = [
    {"type": "deletion", "ruleset_id": 14616733},
    {"type": "non_fast_forward", "ruleset_id": 14616733},
    {"type": "pull_request", "parameters": {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": True,
        "required_reviewers": [],
        "require_code_owner_review": False,
        "dismissal_restriction": {"enabled": False, "allowed_actors": []},
        "require_last_push_approval": True,
        "required_review_thread_resolution": True,
        "require_extra_approval_for_unattributed_changes": True,
        "allowed_merge_methods": ["squash"],
    }, "ruleset_id": 14616733},
    {"type": "required_linear_history", "ruleset_id": 14616733},
    {"type": "update", "ruleset_id": 14616733},
    {"type": "creation", "ruleset_id": 14616733},
]
_ELM_DETAIL: dict[str, Any] = {
    "id": 14616733,
    "enforcement": "active",
    "bypass_actors": [
        {"actor_id": None, "actor_type": "OrganizationAdmin", "bypass_mode": "always"},
        {"actor_id": 8015732, "actor_type": "Team", "bypass_mode": "always"},
        {"actor_id": 18646578, "actor_type": "Team", "bypass_mode": "always"},
    ],
}


def _classic(count: int = 1, *, code_owner: bool = False, last_push: bool = False) -> tuple[int, object]:
    return _OK, {"required_pull_request_reviews": {
        "required_approving_review_count": count,
        "require_code_owner_reviews": code_owner,
        "require_last_push_approval": last_push,
    }}


def _rules(count: int = 1, *, code_owner: bool = False, last_push: bool = False) -> tuple[int, object]:
    return _OK, [{"type": "pull_request", "ruleset_id": 7, "parameters": {
        "required_approving_review_count": count,
        "require_code_owner_review": code_owner,
        "require_last_push_approval": last_push,
    }}]


def _sources(classic: tuple[int, object], rules: tuple[int, object]) -> dict[str, tuple[int, object]]:
    return {_CLASSIC: classic, _RULES: rules}


def _detail(enforcement: str = "active", bypass: list[dict[str, Any]] | None = None) -> tuple[int, object]:
    return _OK, {"enforcement": enforcement, "bypass_actors": bypass or []}


def _integration(actor_id: int, mode: str = "always") -> dict[str, Any]:
    return {"actor_id": actor_id, "actor_type": "Integration", "bypass_mode": mode}


# --- parsing: classic protection and rulesets ---------------------------------- #


def test_classic_protection_with_approving_reviews_requires_a_review() -> None:
    requirement = gate.parse_classic_protection(_classic(2)[1])
    assert requirement == gate.ReviewRequirement(approving_reviews=2)
    assert requirement.requires_review


def test_classic_protection_without_pull_request_reviews_requires_nothing() -> None:
    requirement = gate.parse_classic_protection({"required_status_checks": {"strict": True}})
    assert requirement == gate.ReviewRequirement()
    assert not requirement.requires_review


def test_classic_code_owner_review_requires_a_review() -> None:
    requirement = gate.parse_classic_protection(_classic(0, code_owner=True)[1])
    assert requirement.code_owner_review and requirement.requires_review


def test_last_push_approval_alone_requires_a_review() -> None:
    """Elm's ruleset: count 0, and the last push must be approved. That still needs an outside approver."""
    requirement = gate.parse_classic_protection(_classic(0, last_push=True)[1])
    assert requirement.last_push_approval and requirement.approving_reviews == 0
    assert requirement.requires_review


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


def test_ruleset_ids_are_the_distinct_ids_of_the_pull_request_rules() -> None:
    assert gate.ruleset_ids(_ELM_RULES) == [_ELM_RULESET]
    assert gate.ruleset_ids({"message": "not a list"}) == []


def test_ruleset_payload_that_is_not_a_list_is_rejected() -> None:
    with pytest.raises(ValueError, match="not a list"):
        gate.scope_rules({"message": "unexpected"}, {}, _APP_ID)


# --- elm: the exact payload that motivated the gate ----------------------------- #


def test_elm_main_ruleset_requires_a_review_from_someone_else() -> None:
    """Elm's live ruleset, with its detail read: the daemon cannot satisfy it, so the trap is external."""
    sources = {
        _CLASSIC: (_NOT_FOUND, None),
        _RULES: (_OK, _ELM_RULES),
        gate.ruleset_source_key(_ELM_RULESET): (_OK, _ELM_DETAIL),
    }
    reading = gate.read_protection(sources, _APP_ID)
    assert reading.requirement.last_push_approval and reading.requirement.requires_review
    assert reading.unreadable == () and reading.unverified == ()
    state, message = gate.judge(reading, _APP, "main")
    assert state == gate.GATE_EXTERNAL
    assert message.startswith("Merges need an approving review from someone other than vivi-coordinare.")


def test_elm_main_ruleset_with_its_detail_unreadable_is_still_external_and_noted() -> None:
    sources = {
        _CLASSIC: (_NOT_FOUND, None),
        _RULES: (_OK, _ELM_RULES),
        gate.ruleset_source_key(_ELM_RULESET): (_FORBIDDEN, None),
    }
    reading = gate.read_protection(sources, _APP_ID)
    assert gate.judge(reading, _APP, "main")[0] == gate.GATE_EXTERNAL
    assert reading.unverified == (_ELM_RULESET,)


# --- enforcement and bypass ----------------------------------------------------- #


def _read_with(detail: tuple[int, object], app_id: int | None = _APP_ID) -> gate.ProtectionReading:
    sources = {
        _CLASSIC: (_NOT_FOUND, None),
        _RULES: _rules(1),
        gate.ruleset_source_key("7"): detail,
    }
    return gate.read_protection(sources, app_id)


def test_an_active_ruleset_the_daemon_cannot_bypass_counts() -> None:
    assert gate.judge(_read_with(_detail("active")), _APP, "main")[0] == gate.GATE_EXTERNAL


def test_a_ruleset_in_evaluate_mode_does_not_count() -> None:
    reading = _read_with(_detail("evaluate"))
    assert reading.inactive == ("7",)
    assert not reading.requirement.requires_review
    assert gate.judge(reading, _APP, "main")[0] == gate.GATE_CLEAR


def test_a_disabled_ruleset_does_not_count() -> None:
    reading = _read_with(_detail("disabled"))
    assert gate.judge(reading, _APP, "main")[0] == gate.GATE_CLEAR


def test_the_daemon_app_as_an_always_bypass_actor_makes_the_condition_clear() -> None:
    reading = _read_with(_detail("active", [_integration(_APP_ID)]))
    assert reading.bypassed == ("7",)
    assert gate.judge(reading, _APP, "main")[0] == gate.GATE_CLEAR


def test_the_daemon_app_as_a_pull_request_bypass_actor_makes_the_condition_clear() -> None:
    reading = _read_with(_detail("active", [_integration(_APP_ID, "pull_request")]))
    assert gate.judge(reading, _APP, "main")[0] == gate.GATE_CLEAR


def test_another_app_as_a_bypass_actor_does_not_clear_the_condition() -> None:
    reading = _read_with(_detail("active", [_integration(_APP_ID + 1)]))
    assert reading.bypassed == ()
    assert gate.judge(reading, _APP, "main")[0] == gate.GATE_EXTERNAL


def test_organisation_admin_and_team_bypass_are_not_taken_as_the_daemon() -> None:
    bypass = [
        {"actor_id": None, "actor_type": "OrganizationAdmin", "bypass_mode": "always"},
        {"actor_id": 8015732, "actor_type": "Team", "bypass_mode": "always"},
    ]
    assert gate.judge(_read_with(_detail("active", bypass)), _APP, "main")[0] == gate.GATE_EXTERNAL


def test_without_a_known_app_id_an_integration_bypass_is_not_matched() -> None:
    reading = _read_with(_detail("active", [_integration(_APP_ID)]), app_id=None)
    assert gate.judge(reading, _APP, "main")[0] == gate.GATE_EXTERNAL


def test_an_unreadable_ruleset_detail_is_kept_and_named_not_dropped() -> None:
    reading = _read_with((_FORBIDDEN, None))
    assert reading.unverified == ("7",)
    assert reading.unreadable == ()
    assert gate.judge(reading, _APP, "main")[0] == gate.GATE_EXTERNAL


def test_a_malformed_ruleset_detail_is_kept_and_named() -> None:
    reading = _read_with((_OK, ["not", "an", "object"]))
    assert reading.unverified == ("7",)
    assert gate.judge(reading, _APP, "main")[0] == gate.GATE_EXTERNAL


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


def _facts(decision: str = "", merge_state: str = "BLOCKED", author: str = "vivi-coordinare",
           checks: str = "SUCCESS") -> gate.PrFacts:
    return gate.PrFacts(review_decision=decision, merge_state_status=merge_state,
                        author_login=author, checks_state=checks)


@pytest.mark.parametrize(
    ("facts", "condition", "expected"),
    [
        # GitHub says a review is required: the daemon's own PR waits on it.
        (_facts("REVIEW_REQUIRED"), None, True),
        (_facts("REVIEW_REQUIRED", author="Vivi-Coordinare[bot]"), None, True),
        (_facts("REVIEW_REQUIRED", author="jason"), None, False),
        (_facts("REVIEW_REQUIRED", merge_state="CLEAN"), None, False),
        (_facts("APPROVED"), None, False),
        (_facts("REVIEW_REQUIRED", author=""), None, False),
        # Elm: an empty reviewDecision on a BLOCKED daemon PR, tagged by the symphony condition.
        (_facts(""), gate.GATE_EXTERNAL, True),
        (_facts("", checks="PENDING"), gate.GATE_EXTERNAL, True),
        (_facts("", checks=""), gate.GATE_EXTERNAL, True),
        (_facts("", author="jason"), gate.GATE_EXTERNAL, False),
        (_facts("", merge_state="CLEAN"), gate.GATE_EXTERNAL, False),
        (_facts(""), gate.GATE_CLEAR, False),
        (_facts(""), gate.GATE_UNREADABLE, False),
        (_facts(""), None, False),
        # Failing checks are a checks wait, never a review tag, whatever the rest says.
        (_facts("REVIEW_REQUIRED", checks="FAILURE"), None, False),
        (_facts("", checks="FAILURE"), gate.GATE_EXTERNAL, False),
        (_facts("", checks="ERROR"), gate.GATE_EXTERNAL, False),
        (_facts("REVIEW_REQUIRED", checks="error"), None, False),
    ],
)
def test_pr_waits_on_external_review_only_for_the_daemon_blocked_pr_with_no_failing_checks(
    facts: gate.PrFacts, condition: str | None, expected: bool,
) -> None:
    assert gate.pr_waits_on_external_review(facts, condition=condition, daemon_logins=_APP) is expected


# --- refresh: startup, cadence and dedupe across cycles ------------------------- #


class _Notifier:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def dispatch(self, event: Any) -> None:
        self.events.append(event)


class _Github:
    """Stands in for GitHubService: protection sources, the App id and the daemon identity."""

    def __init__(self, classic: tuple[int, object], rules: tuple[int, object],
                 logins: frozenset[str] = _APP, app_id: int | None = _APP_ID) -> None:
        self.sources = _sources(classic, rules)
        self.logins = logins
        self.app_id = app_id
        self.reads = 0

    async def get_branch_protection_sources(self, branch: str) -> dict[str, tuple[int, object]]:
        self.reads += 1
        return self.sources

    async def daemon_app_id(self) -> int | None:
        return self.app_id

    async def self_logins(self) -> frozenset[str]:
        return self.logins


class _BrokenGithub:
    async def get_branch_protection_sources(self, branch: str) -> dict[str, tuple[int, object]]:
        msg = "boom"
        raise RuntimeError(msg)

    async def daemon_app_id(self) -> int | None:
        return _APP_ID

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
async def test_elm_shaped_repository_surfaces_the_trap_at_startup() -> None:
    sym = SymphonyRuntimeState(name="elm")
    github = _Github((_NOT_FOUND, None), (_OK, _ELM_RULES))
    github.sources[gate.ruleset_source_key(_ELM_RULESET)] = (_OK, _ELM_DETAIL)
    notifier, log = _Notifier(), ActivityLog()
    await _refresh(sym, github, notifier, log, now=1000.0)
    assert sym.merge_review is not None and sym.merge_review["state"] == gate.GATE_EXTERNAL
    assert len(notifier.events) == 1


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
async def test_a_bypassed_trap_reads_clear_and_is_silent() -> None:
    sym = SymphonyRuntimeState(name="garden")
    github = _Github((_NOT_FOUND, None), (_OK, _ELM_RULES))
    github.sources[gate.ruleset_source_key(_ELM_RULESET)] = (
        _OK, {"enforcement": "active", "bypass_actors": [_integration(_APP_ID)]},
    )
    notifier, log = _Notifier(), ActivityLog()
    await _refresh(sym, github, notifier, log, now=1000.0)
    assert sym.merge_review is not None and sym.merge_review["state"] == gate.GATE_CLEAR
    assert notifier.events == [] and log.snapshot() == []


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
async def test_a_notifier_that_raises_does_not_break_the_condition() -> None:
    class _Broken:
        async def dispatch(self, event: Any) -> None:
            msg = "smtp down"
            raise RuntimeError(msg)

    sym = SymphonyRuntimeState(name="garden")
    probe = gate.ReviewProbe(
        symphony="garden", branch="main", github=_Github(_classic(1), (_OK, [])),
        notification_service=_Broken(), activity=ActivityLog(),
    )
    await gate.refresh_merge_review(sym, probe, now=1000.0)
    assert sym.merge_review is not None and sym.merge_review["state"] == gate.GATE_EXTERNAL


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


def _mock_http(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "coordinare.services.github.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )


@pytest.mark.asyncio
async def test_rest_read_asks_for_both_sources_and_each_named_ruleset(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/protection"):
            return httpx.Response(_NOT_FOUND, json={"message": "Branch not protected"})
        if request.url.path.endswith("/rulesets/14616733"):
            return httpx.Response(_OK, json=_ELM_DETAIL)
        return httpx.Response(_OK, json=_ELM_RULES)

    _mock_http(monkeypatch, handler)
    service = _service()
    service._project_name = "elm"
    sources = await service.get_branch_protection_sources("main")
    assert sources[_CLASSIC][0] == _NOT_FOUND and sources[_RULES][0] == _OK
    assert sources[gate.ruleset_source_key(_ELM_RULESET)] == (_OK, _ELM_DETAIL)
    assert seen == [
        "/repos/ViviDynamics/elm/rules/branches/main",
        "/repos/ViviDynamics/elm/branches/main/protection",
        "/repos/ViviDynamics/elm/rulesets/14616733",
    ]
    assert gate.read_protection(sources, _APP_ID).requirement.requires_review


@pytest.mark.asyncio
async def test_rest_read_reports_a_forbidden_source_without_raising(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(_FORBIDDEN, json={"message": "Resource not accessible"})

    _mock_http(monkeypatch, handler)
    service = _service()
    service._project_name = "garden"
    sources = await service.get_branch_protection_sources("main")
    assert sources[_CLASSIC][0] == _FORBIDDEN and sources[_RULES][0] == _FORBIDDEN
    assert gate.read_protection(sources, _APP_ID).unreadable == (_CLASSIC, _RULES)


@pytest.mark.asyncio
async def test_rest_read_without_a_repository_name_reports_both_sources_unreadable() -> None:
    sources = await _service().get_branch_protection_sources("main")
    assert sources == {_CLASSIC: (0, None), _RULES: (0, None)}


@pytest.mark.asyncio
async def test_rest_read_of_a_server_error_gives_up_as_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    _mock_http(monkeypatch, handler)
    service = _service()
    service._project_name = "garden"
    sources = await service.get_branch_protection_sources("main")
    assert sources == {_CLASSIC: (0, None), _RULES: (0, None)}


@pytest.mark.asyncio
async def test_a_token_has_no_app_id() -> None:
    assert await _service().daemon_app_id() is None


# --- monitor_pr: the card's wait reason ------------------------------------------ #


class _PrGithub:
    def __init__(self, *, decision: str, merge_state: str, author: str, checks: str = "SUCCESS") -> None:
        self._ctx = {
            "reviews": [], "review_threads": [], "head_oid": "a" * 40, "review_decision": decision,
            "merge_state_status": merge_state, "author_login": author, "checks_state": checks,
            "state": "OPEN",
        }

    async def get_pr_review_context(self, pr_id: str) -> dict[str, Any]:
        return self._ctx

    async def self_logins(self) -> frozenset[str]:
        return _APP


def _pr_state(github: _PrGithub, condition: str | None = None) -> dict:
    state = initial_state()
    state["current_card"] = {"pr_node_id": "PR_101", "id": "CARD_101", "title": "solo"}
    state["human_reviewers"] = ["jason"]
    state["github_service"] = github
    state["lifecycle_completed_at"] = None
    state["merge_review_condition"] = condition
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
async def test_elm_shaped_blocked_pr_with_an_empty_review_decision_is_tagged_by_the_condition() -> None:
    state = _pr_state(
        _PrGithub(decision="", merge_state="BLOCKED", author="vivi-coordinare"),
        condition=gate.GATE_EXTERNAL,
    )
    result = await monitor_pr(state)
    assert result["review_wait_reason"] == "external_review_required"


@pytest.mark.asyncio
async def test_empty_review_decision_without_the_condition_keeps_the_generic_wait() -> None:
    state = _pr_state(_PrGithub(decision="", merge_state="BLOCKED", author="vivi-coordinare"), condition=gate.GATE_CLEAR)
    result = await monitor_pr(state)
    assert result["review_wait_reason"] == "no_human_approval"


@pytest.mark.asyncio
async def test_a_blocked_pr_with_failing_checks_is_not_tagged_as_an_external_review() -> None:
    state = _pr_state(
        _PrGithub(decision="", merge_state="BLOCKED", author="vivi-coordinare", checks="FAILURE"),
        condition=gate.GATE_EXTERNAL,
    )
    result = await monitor_pr(state)
    assert result["review_wait_reason"] == "no_human_approval"


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
