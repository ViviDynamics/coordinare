"""#142 review: the classifier, the exhaustion gate, closed PRs in any phase, own comments, and the live fingerprint.

Each test here pins one finding from the independent review of PR #214. The
classifier must work on text that secret redaction has already rewritten, so
the samples are checked raw and redacted.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from coordinare.dashboard.store import DashboardStore
from coordinare.graph.nodes import route_issue_comments as route_module
from coordinare.graph.nodes.route_issue_comments import route_issue_comments
from coordinare.graph.state import initial_state
from coordinare.lib.redaction import redact_secrets
from coordinare.services.assessor_failure import describe_format_exhaustion
from coordinare.services.block_cause import (
    CAUSE_CLOSED_PR,
    CAUSE_FORMAT_EXHAUSTED,
    CAUSE_OUTPUT_TRUNCATED,
    CAUSE_SYSTEM_ERROR,
    OUTPUT_CAP_REASON,
    block_cause,
)
from coordinare.services.card_identity import CardIdentityMap
from coordinare.services.closed_pr import CLOSED_PR_REASON_PREFIX
from coordinare.services.issue_comment_service import is_coordinare_author

_DOCS = Path(__file__).resolve().parents[2] / "docs" / "operators" / "ci-infrastructure-holds.md"


def _stopped(reason: str, **fields: Any) -> dict[str, Any]:
    return {"phase": "blocked", "system_error_reason": reason, **fields}


# --- 1. a bare word is not a truncation ------------------------------------------------ #


@pytest.mark.parametrize("reason", [
    "Performer dispatch failed (implementing): docker logs truncated after 4000 lines",
    "the review could not complete (no code defect):\nHTTP 502 response truncated by proxy",
])
def test_a_bare_truncated_word_is_a_system_error_not_a_token_cap(reason: str) -> None:
    """Both false positives from the review: a log or proxy saying 'truncated' is not the model's output budget."""
    assert block_cause(_stopped(reason)) == CAUSE_SYSTEM_ERROR


def test_the_token_cap_stop_is_named_output_truncated() -> None:
    """The real truncation stop sets its own fixed reason, so the chip can name it."""
    assert block_cause(_stopped(OUTPUT_CAP_REASON)) == CAUSE_OUTPUT_TRUNCATED


def test_an_explicit_finish_reason_length_is_output_truncated() -> None:
    assert block_cause(_stopped("The model stopped with finish_reason=length")) == CAUSE_OUTPUT_TRUNCATED


def test_a_format_failure_whose_structured_part_says_truncated_is_output_truncated() -> None:
    reason = "BACKEND_FORMAT_ERROR: the answer was truncated before the object closed"
    assert block_cause(_stopped(reason, system_error_notified=True)) == CAUSE_OUTPUT_TRUNCATED


# --- 2. retries exhausted needs the exhaustion signal ----------------------------------- #


def test_a_retrying_format_failure_is_not_named_retries_exhausted() -> None:
    reason = describe_format_exhaustion(
        "BACKEND_FORMAT_ERROR: Expecting value at line 1", stage="implementing", count=1, identical=True,
    )
    assert block_cause(_stopped(reason, system_error_notified=False)) == CAUSE_SYSTEM_ERROR


def test_an_exhausted_format_failure_is_named_retries_exhausted() -> None:
    reason = describe_format_exhaustion(
        "BACKEND_FORMAT_ERROR: Expecting value at line 1", stage="implementing", count=3, identical=True,
    )
    assert block_cause(_stopped(reason, system_error_notified=True)) == CAUSE_FORMAT_EXHAUSTED


# --- 3. a closed PR is named whatever the phase -------------------------------------------- #


def test_a_closed_pr_with_owned_writers_keeps_its_reason() -> None:
    """monitor_pr parks a closed PR in monitoring_performer while a writer is still owned."""
    session = {"phase": "monitoring_performer", "system_error_reason": f"{CLOSED_PR_REASON_PREFIX} https://x/pull/9. Reopen."}
    assert block_cause(session) == CAUSE_CLOSED_PR


# --- every classification holds on redacted text (#213 redacts at write) -------------------- #


_SAMPLES = [
    f"{CLOSED_PR_REASON_PREFIX} https://x-access-token:ghp_abcdefghijklmnopqrstuvwxyz0123456789@github.com/o/r/pull/9. Reopen.",
    describe_format_exhaustion("BACKEND_FORMAT_ERROR: Expecting value token=sk-abcdef1234567890abcdef",
                               stage="implementing", count=3, identical=True),
    "The model stopped with finish_reason=length",
    OUTPUT_CAP_REASON,
    "Performer dispatch failed (implementing): docker logs truncated after 4000 lines",
]


@pytest.mark.parametrize("raw", _SAMPLES)
def test_the_classification_survives_secret_redaction(raw: str) -> None:
    redacted = redact_secrets(raw)
    assert block_cause(_stopped(raw, system_error_notified=True)) == block_cause(
        _stopped(redacted, system_error_notified=True),
    )


# --- 4. coordinare's own comments are never human input ------------------------------------- #


def test_the_coordinare_app_is_recognised_under_both_logins() -> None:
    assert is_coordinare_author("vivi-coordinare")
    assert is_coordinare_author("vivi-coordinare[bot]")
    assert not is_coordinare_author("alice")
    assert not is_coordinare_author("vivi-coordinare-fan")


class _GitHub(CardIdentityMap):
    """The comment read the route node makes, as the existing route tests stub it."""

    def __init__(self, comments: list[dict[str, Any]]) -> None:
        self._comments = comments
        self._remember_issue_numbers({"ITEM_1": 42})

    async def get_issue_details(self, card_id: str) -> dict[str, Any]:
        return {}

    async def get_issue_comments(self, issue_number: int, since_id: int | None = None) -> list[dict[str, Any]]:
        return [c for c in self._comments if since_id is None or c["id"] > since_id]


_OLD_TEXT = "**Waiting on #42**: unresolvable dependency\n\nAdd them to the board."
_NEW_TEXT = "**💻 Implementer** · Needs human:\n- Which provider should we use?"


@pytest.mark.asyncio
@pytest.mark.parametrize("author", ["vivi-coordinare", "vivi-coordinare[bot]"])
@pytest.mark.parametrize("text", [_OLD_TEXT, _NEW_TEXT])
async def test_a_coordinare_comment_is_skipped_whatever_its_text(
    monkeypatch: pytest.MonkeyPatch, author: str, text: str,
) -> None:
    classify = AsyncMock(return_value="clarification")
    monkeypatch.setattr(route_module, "classify_issue_comment_ai", classify)
    state = initial_state()
    state["github_service"] = _GitHub([{"id": 5001, "author": author, "body": text, "created_at": "2026-10-14T10:00:00Z"}])
    state["current_card"] = {"id": "ITEM_1", "issue_number": 42}

    result = await route_issue_comments(state)

    assert classify.await_count == 0
    assert (result.get("card_clarifications") or []) == []
    assert 5001 not in (result.get("processed_issue_comment_ids") or ())


@pytest.mark.asyncio
async def test_a_human_comment_with_the_needs_human_text_is_still_routed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(route_module, "classify_issue_comment_ai", AsyncMock(return_value=None))
    state = initial_state()
    state["github_service"] = _GitHub([{"id": 5002, "author": "alice", "body": _NEW_TEXT, "created_at": "2026-10-14T10:00:00Z"}])
    state["current_card"] = {"id": "ITEM_1", "issue_number": 42}

    result = await route_issue_comments(state)

    assert [c["comment_id"] for c in result["card_clarifications"]] == [5002]


@pytest.mark.asyncio
async def test_a_human_comment_with_the_old_waiting_text_is_still_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    """A human may write 'Waiting on #42'. It is classified as a blocker update, as before, not dropped."""
    classify = AsyncMock(return_value=None)
    monkeypatch.setattr(route_module, "classify_issue_comment_ai", classify)
    state = initial_state()
    state["github_service"] = _GitHub([{"id": 5003, "author": "alice", "body": _OLD_TEXT, "created_at": "2026-10-14T10:00:00Z"}])
    state["current_card"] = {"id": "ITEM_1", "issue_number": 42}

    result = await route_issue_comments(state)

    assert classify.await_count == 1
    assert (result.get("card_clarifications") or []) == []
    assert 5003 in result["processed_issue_comment_ids"]


# --- 5. the operator doc uses the new wording --------------------------------------------- #


def test_the_ci_holds_doc_names_the_new_kinds() -> None:
    text = _DOCS.read_text(encoding="utf-8")
    assert "Needs human, operator action required" in text
    assert "System blocked" not in text
    assert "Needs input" not in text


# --- 7. the live fingerprint sees a cause change --------------------------------------------- #


def _fingerprint(session: dict[str, Any]) -> tuple[Any, ...]:
    daemon = SimpleNamespace(state={"active_sessions": {"PVTI_1": session}})
    return DashboardStore._active_sessions_fingerprint(daemon)


def test_a_change_of_system_error_reason_changes_the_fingerprint() -> None:
    base = {"phase": "blocked", "system_error_reason": "first"}
    assert _fingerprint(base) != _fingerprint({"phase": "blocked", "system_error_reason": "second"})


def test_an_environment_hold_changes_the_fingerprint() -> None:
    base = {"phase": "blocked"}
    assert _fingerprint(base) != _fingerprint({"phase": "blocked", "env_blocked": {"pattern_id": "p"}})
