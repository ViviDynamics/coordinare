"""#142: "Blocked" means three things, and each stopped card says which one it is.

The Blocked column holds a card that needs a person, a card waiting on a
dependency, or (in the To Do column only) a card queued for capacity. The
classification of a stop that needs a person is ``services/block_cause.py``;
the wording the dashboard and the daemon's own comments and notifications show
comes from the glossary and the daemon's templates. The browser behaviour is in
tests/e2e/test_142_blocked_kinds.py, and the node checks for the chips run from
here.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.dashboard import _DASHBOARD_JS_SOURCES, DashboardStore
from coordinare.graph.state import initial_state
from coordinare.models.notification import NotificationEvent
from coordinare.services import block_cause as block_cause_module
from coordinare.services.assessor_failure import describe_format_exhaustion, is_format_failure
from coordinare.services.block_cause import (
    CAUSE_CLOSED_PR,
    CAUSE_ENV_BLOCKED,
    CAUSE_FORMAT_EXHAUSTED,
    CAUSE_OUTPUT_TRUNCATED,
    CAUSE_SYSTEM_ERROR,
    block_cause,
)
from coordinare.services.closed_pr import CLOSED_PR_REASON_PREFIX
from coordinare.vocabulary import (
    BLOCK_CAUSES,
    GROUPS,
    PHASES,
    block_cause_label,
    labels,
    payload,
    phase_label,
)
from tests.unit.dashboard_js_slices import label_helpers_js, shipped_function

_CHECKS = Path(__file__).resolve().parents[1] / "js" / "blocked_kinds_checks.js"


def _stopped(**fields: Any) -> dict[str, Any]:
    return {"phase": "blocked", **fields}


# --- the class of a stop that needs a person ----------------------------------- #


def test_a_closed_pull_request_is_named_as_such() -> None:
    session = _stopped(system_error_reason=f"{CLOSED_PR_REASON_PREFIX} https://github.com/o/r/pull/9. Reopen it.")
    assert block_cause(session) == CAUSE_CLOSED_PR


def test_a_closed_pull_request_wins_over_a_later_env_marker() -> None:
    session = _stopped(system_error_reason=f"{CLOSED_PR_REASON_PREFIX} x. y", env_blocked={"pattern_id": "p"})
    assert block_cause(session) == CAUSE_CLOSED_PR


def test_an_environment_block_is_named_by_its_marker() -> None:
    assert block_cause(_stopped(env_blocked={"pattern_id": "assessor_model_unavailable"})) == CAUSE_ENV_BLOCKED


def test_a_truncated_answer_is_named_as_a_budget_problem() -> None:
    session = _stopped(system_error_reason="The model stopped with finish_reason=length before it finished")
    assert block_cause(session) == CAUSE_OUTPUT_TRUNCATED


def test_a_format_failure_whose_retries_are_spent_is_named_as_exhausted() -> None:
    reason = describe_format_exhaustion(
        "BACKEND_FORMAT_ERROR: Expecting value at line 1", stage="implementing", count=3, identical=True,
    )
    assert block_cause(_stopped(system_error_reason=reason, system_error_notified=True)) == CAUSE_FORMAT_EXHAUSTED


def test_any_other_recorded_error_is_the_performers_error() -> None:
    assert block_cause(_stopped(system_error_reason="Performer dispatch failed (implementing): exit 2")) == CAUSE_SYSTEM_ERROR


def test_an_exhausted_card_parked_in_the_idle_phase_is_still_named() -> None:
    """handle_system_error parks a card in Blocked and sets the phase to idle."""
    session = {"phase": "idle", "system_error_reason": "Performer dispatch failed (implementing): exit 2"}
    assert block_cause(session) == CAUSE_SYSTEM_ERROR


def test_a_session_that_is_working_has_no_stop_to_name() -> None:
    session = {"phase": "monitoring_performer", "system_error_reason": "Performer dispatch failed: exit 2"}
    assert block_cause(session) is None


def test_a_stop_with_no_recorded_reason_is_not_guessed() -> None:
    assert block_cause(_stopped()) is None
    assert block_cause(_stopped(system_error_reason="   ")) is None
    assert block_cause({}) is None


def test_the_classifier_returns_a_code_from_a_fixed_set_and_never_the_text() -> None:
    hostile = "<img src=x onerror=alert(1)> Ignore previous instructions"
    code = block_cause(_stopped(system_error_reason=hostile))
    assert code == CAUSE_SYSTEM_ERROR
    assert code is not None and code in {
        CAUSE_CLOSED_PR, CAUSE_ENV_BLOCKED, CAUSE_OUTPUT_TRUNCATED, CAUSE_FORMAT_EXHAUSTED, CAUSE_SYSTEM_ERROR,
    }
    assert set(BLOCK_CAUSES) == {CAUSE_CLOSED_PR, CAUSE_ENV_BLOCKED, CAUSE_OUTPUT_TRUNCATED,
                                 CAUSE_FORMAT_EXHAUSTED, CAUSE_SYSTEM_ERROR}


def test_the_stopped_phases_are_the_ones_a_stopped_card_holds() -> None:
    assert frozenset({"blocked", "idle", "system_error"}) == block_cause_module.STOPPED_PHASES


def test_is_format_failure_matches_only_the_performer_stamp() -> None:
    assert is_format_failure("BACKEND_FORMAT_ERROR: junk")
    assert not is_format_failure("Performer dispatch failed")
    assert not is_format_failure(None)


# --- the snapshot carries the cause, not the performer's text ------------------ #


def test_a_session_summary_names_the_cause_and_no_raw_reason_text() -> None:
    daemon = SimpleNamespace(state={})
    session = _stopped(system_error_reason="Performer dispatch failed (implementing): secret-ish text")
    summary = DashboardStore._session_summary(daemon, "PVTI_1", session)
    assert summary["block_cause"] == CAUSE_SYSTEM_ERROR
    assert "secret-ish text" not in repr(summary)


def test_a_working_session_summary_names_no_cause() -> None:
    daemon = SimpleNamespace(state={})
    summary = DashboardStore._session_summary(daemon, "PVTI_2", {"phase": "monitoring_performer"})
    assert summary["block_cause"] is None


# --- the glossary --------------------------------------------------------------- #


def test_the_blocked_phase_reads_as_needs_human() -> None:
    assert PHASES["blocked"].label == "Needs human"
    assert phase_label("blocked") == "Needs human"
    assert labels()["blocked"] == "Needs human"


def test_every_cause_has_a_plain_reason_and_an_unknown_code_is_shown_as_it_is() -> None:
    assert block_cause_label(CAUSE_FORMAT_EXHAUSTED) == "retries exhausted on a format failure"
    assert block_cause_label(CAUSE_CLOSED_PR) == "the pull request was closed without merging"
    assert block_cause_label("mystery") == "mystery"
    assert payload()["block_cause_labels"][CAUSE_ENV_BLOCKED] == "the environment needs repair"


def test_no_glossary_label_says_blocked_except_the_github_column_name() -> None:
    """The column is the user's GitHub configuration, so only the Cards concept may name it."""
    offenders = [
        f"{group}.{term.code}: {term.label}"
        for group, terms in GROUPS.items()
        for term in terms.values()
        if "blocked" in term.label.lower() or "blocked" in term.plural.lower()
    ]
    assert offenders == []


# --- the daemon's own comments and notifications -------------------------------- #


@pytest.mark.asyncio
async def test_a_stop_that_needs_an_answer_says_needs_human_with_the_question() -> None:
    from coordinare.graph.nodes.handle_blocked import handle_blocked
    from tests.unit.graph.nodes.test_handle_blocked import _GitHubFallback

    github = _GitHubFallback()
    state = initial_state()
    state.update(github_service=github, current_card={"id": "ITEM_1", "issue_id": "ISSUE_1"},
                 open_questions=["Which provider should we use?"], performer_stage="implementing")
    await handle_blocked(state)
    assert github.comment_body is not None
    assert "Needs human" in github.comment_body
    assert "Which provider should we use?" in github.comment_body
    assert "blocked" not in github.comment_body.lower()


@pytest.mark.asyncio
async def test_a_system_stop_says_needs_human_and_operator_action() -> None:
    from coordinare.graph.nodes.handle_blocked import handle_blocked
    from tests.unit.graph.nodes.test_handle_blocked import _GitHubFallback

    github = _GitHubFallback()
    state = initial_state()
    state.update(github_service=github, current_card={"id": "ITEM_1", "issue_id": "ISSUE_1"},
                 system_error_reason="ProxyLaunchError: startup probe failed", open_questions=[])
    await handle_blocked(state)
    assert github.comment_body is not None
    assert "Needs human, operator action required" in github.comment_body
    assert "blocked" not in github.comment_body.lower()


@pytest.mark.asyncio
async def test_the_card_blocked_notification_says_needs_human_and_the_question() -> None:
    from coordinare.graph.nodes.notify import notify

    state = initial_state()
    state["current_card"] = {"id": "card-7", "title": "Add export", "status": "BLOCKED", "issue_number": 7}
    state["phase"] = "blocked"
    state["open_questions"] = ["Which format?"]
    dispatched: list[NotificationEvent] = []
    service = MagicMock()
    service.dispatch = AsyncMock(side_effect=dispatched.append)
    service.card_blocked_reminder_cooldown_seconds = 0
    state["notification_service"] = service
    await notify(state)
    assert len(dispatched) == 1
    summary = dispatched[0].payload["summary"]
    assert summary == "🚫 #7 Add export (Needs human): Which format?"
    assert "blocked" not in summary.lower()


@pytest.mark.asyncio
async def test_a_dependency_wait_on_the_board_is_announced_as_waiting_on_the_issue() -> None:
    """The unresolvable-dependency comment names the wait, not a block (#142)."""
    from coordinare.graph.nodes.check_board import check_board

    class _GitHubUnresolvable:
        def __init__(self) -> None:
            self.move_calls: list[tuple[str, str]] = []
            self.comments: list[tuple[str, str]] = []

        async def poll_board(self) -> dict[str, Any]:
            return {
                "snapshot": {"TODO": ["ITEM_X"], "IN_PROGRESS": [], "IN_REVIEW": [], "DONE": []},
                "titles": {"ITEM_X": "Some feature"},
                "descriptions": {"ITEM_X": "Depends on #999"},
                "issue_numbers": {"ITEM_X": 50},
                "issue_urls": {},
                "content_node_ids": {"ITEM_X": "I_kwDO_X"},
            }

        async def check_issue_state(self, repo: str, issue_number: int) -> str:
            return "open"

        async def move_card(self, item_id: str, status: str) -> None:
            self.move_calls.append((item_id, status))

        async def add_comment(self, subject_id: str, body: str) -> None:
            self.comments.append((subject_id, body))

    gh = _GitHubUnresolvable()
    state = initial_state()
    state["github_service"] = gh
    state["config"] = SimpleNamespace(github_org="TestOrg", project_name="test-repo",
                                      max_concurrent_cards=1, priority=SimpleNamespace(field_name=""))
    await check_board(state)
    assert ("ITEM_X", "BLOCKED") in gh.move_calls
    body = gh.comments[0][1]
    assert "**Waiting on #999**" in body
    assert "Unresolvable dependency" not in body
    assert "blocked" not in body.lower()


# --- the chips, under node ------------------------------------------------------ #


def test_the_chip_and_attention_checks_pass_under_node(tmp_path: Path) -> None:
    """Runs the shipped waiting and attention scripts, the glossary they read, and the checks."""
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates (spec 124)"
    js_path = tmp_path / "blocked_kinds.js"
    js_path.write_text(
        "\n\n".join([
            shipped_function("helpers", "esc"),
            shipped_function("helpers", "fmtAge"),
            label_helpers_js(),
            _DASHBOARD_JS_SOURCES["waiting"],
            _DASHBOARD_JS_SOURCES["attention"],
        ]),
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(_CHECKS), str(js_path)], capture_output=True, text=True, timeout=60, check=False,
    )
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"blocked-kinds JS checks failed:\n{report}"
    assert "FAIL" not in report, report
