"""Unit tests for advocate_scan node and AdvocateService paths (007, T012/T018/T022/T025)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.models.advocate import IssueType, ScoringProvider
from coordinare.services.scoring import ScoringResult


def _make_issue(
    issue_id: str = "issue-1",
    number: int = 1,
    title: str = "How do I use X?",
    body: str = "I want to understand X.",
    labels: list[str] | None = None,
) -> dict:
    label_nodes = [{"id": f"label-{lbl}", "name": lbl} for lbl in (labels or [])]
    return {
        "id": issue_id,
        "number": number,
        "title": title,
        "body": body,
        "url": f"https://github.com/org/repo/issues/{number}",
        "labels": {"nodes": label_nodes},
    }


def _make_advocate_service(
    issues: list[dict] | None = None,
    file_content: str | None = "# README\nDocumentation content.",
    scorer_result: ScoringResult | None = None,
    label_ids: dict[str, str] | None = None,
) -> MagicMock:
    """Build an AdvocateService with mocked GitHub and Claude."""
    from coordinare.config import AdvocateConfig
    from coordinare.services.advocate import AdvocateService

    github = AsyncMock()
    github.list_open_issues = AsyncMock(return_value=issues or [])
    github.get_file_content = AsyncMock(return_value=file_content)
    github.add_labels = AsyncMock()
    github.add_comment = AsyncMock()

    config = AdvocateConfig(
        enabled=True,
        github_repo="coordinare",
        confidence_threshold=0.70,
        doc_sources=["README.md"],
    )

    default_label_ids = {
        "advocate-handled": "label-handled",
        "needs-human": "label-escalation",
    }

    service = AdvocateService(
        github=github,
        notification_service=None,
        config=config,
        github_org="ViviDynamics",
        label_ids=label_ids or default_label_ids,
        scorers=[],
    )

    if scorer_result is not None:
        mock_scorer = MagicMock()
        mock_scorer.score = AsyncMock(return_value=scorer_result)
        service._scorers = [mock_scorer]

    return service, github


# ---------------------------------------------------------------------------
# T012 — Question / confusion path (US1)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_question_issue_label_applied_before_comment() -> None:
    """New question issue → label applied first, then reply posted."""
    scorer_result = ScoringResult(
        provider=ScoringProvider(provider_name="claude", score=0.85, reasoning=""),
        classification=IssueType.question,
        response_text="Based on `README.md`: the answer is X",
        source_documents=["README.md"],
    )
    service, github = _make_advocate_service(
        issues=[_make_issue()],
        scorer_result=scorer_result,
    )

    await service.scan_and_respond(set())

    # Label must be applied before comment (assert order using call_args_list)
    label_call_idx = None
    comment_call_idx = None
    all_calls = github.method_calls
    for i, call in enumerate(all_calls):
        if call[0] == "add_labels":
            label_call_idx = i
        elif call[0] == "add_comment":
            comment_call_idx = i

    assert label_call_idx is not None, "add_labels was never called"
    assert comment_call_idx is not None, "add_comment was never called"
    assert label_call_idx < comment_call_idx, "label must be applied before comment"


@pytest.mark.asyncio
async def test_reply_comment_contains_disclosure() -> None:
    """Posted comment must contain the disclosure_template text (FR-014)."""
    from coordinare.config import AdvocateConfig
    from coordinare.services.advocate import AdvocateService

    scorer_result = ScoringResult(
        provider=ScoringProvider(provider_name="claude", score=0.85, reasoning=""),
        classification=IssueType.question,
        response_text="Based on `README.md`: answer here",
        source_documents=["README.md"],
    )
    github = AsyncMock()
    github.list_open_issues = AsyncMock(return_value=[_make_issue()])
    github.get_file_content = AsyncMock(return_value="# README\nContent.")
    github.add_labels = AsyncMock()
    github.add_comment = AsyncMock()

    config = AdvocateConfig(enabled=True, github_repo="coordinare")
    mock_scorer = MagicMock()
    mock_scorer.score = AsyncMock(return_value=scorer_result)

    service = AdvocateService(
        github=github,
        notification_service=None,
        config=config,
        github_org="org",
        label_ids={"advocate-handled": "lh", "needs-human": "le"},
        scorers=[mock_scorer],
    )

    await service.scan_and_respond(set())

    comment_calls = [c for c in github.method_calls if c[0] == "add_comment"]
    assert len(comment_calls) >= 1
    posted_body = comment_calls[0][1][1]  # second positional arg
    assert config.disclosure_template in posted_body


@pytest.mark.asyncio
async def test_duplicate_issue_already_in_processed_ids_skipped() -> None:
    """Issue already in processed_ids → skipped, no comment."""
    service, github = _make_advocate_service(issues=[_make_issue(issue_id="issue-1")])

    await service.scan_and_respond({"issue-1"})

    github.add_comment.assert_not_called()


@pytest.mark.asyncio
async def test_already_labeled_issue_skipped() -> None:
    """Issue already carrying advocate-handled label → skipped."""
    issue = _make_issue(labels=["advocate-handled"])
    service, github = _make_advocate_service(issues=[issue])

    await service.scan_and_respond(set())

    github.add_comment.assert_not_called()


@pytest.mark.asyncio
async def test_advocate_service_none_returns_state_unchanged() -> None:
    """If advocate_service is None, advocate_scan node returns state unchanged."""
    from coordinare.graph.nodes.advocate import advocate_scan

    state = {"phase": "idle", "advocate_service": None}
    result = await advocate_scan(state)

    assert result is state
    assert result["phase"] == "idle"


@pytest.mark.asyncio
async def test_advocate_scan_node_calls_service_and_updates_history() -> None:
    """advocate_scan with a live service calls scan_and_respond and stores returned ids."""
    from coordinare.graph.nodes.advocate import advocate_scan

    mock_service = MagicMock()
    mock_service.scan_and_respond = AsyncMock(return_value={"issue-1", "issue-2"})

    state: dict = {
        "phase": "idle",
        "advocate_service": mock_service,
        "advocate_history": set(),
    }
    result = await advocate_scan(state)

    mock_service.scan_and_respond.assert_awaited_once_with(set())
    assert result["advocate_history"] == {"issue-1", "issue-2"}


@pytest.mark.asyncio
async def test_advocate_scan_node_handles_service_exception() -> None:
    """advocate_scan logs and swallows exceptions from scan_and_respond; state returned."""
    from coordinare.graph.nodes.advocate import advocate_scan

    mock_service = MagicMock()
    mock_service.scan_and_respond = AsyncMock(side_effect=RuntimeError("boom"))

    state: dict = {
        "phase": "idle",
        "advocate_service": mock_service,
        "advocate_history": set(),
    }
    result = await advocate_scan(state)

    # State is still returned despite the exception
    assert result is state
    assert result["phase"] == "idle"


# ---------------------------------------------------------------------------
# T018 — Escalation paths (US2)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sensitive_keyword_escalates_before_claude_call() -> None:
    """Sensitive keyword in title → escalation short-circuits before Claude call."""
    issue = _make_issue(title="billing question about my account")
    scorer_result = ScoringResult(
        provider=ScoringProvider(provider_name="claude", score=0.9, reasoning=""),
        classification=IssueType.question,
        response_text="answer",
    )
    service, github = _make_advocate_service(
        issues=[issue],
        scorer_result=scorer_result,
    )
    # scorer should NOT be called
    service._scorers[0].score = AsyncMock()

    await service.scan_and_respond(set())

    # needs-human label should be applied
    github.add_labels.assert_called()
    applied_label_ids = github.add_labels.call_args[0][1]
    assert "label-escalation" in applied_label_ids

    # scorer was not called (short-circuit)
    service._scorers[0].score.assert_not_called()


@pytest.mark.asyncio
async def test_complaint_classification_escalates_regardless_of_confidence() -> None:
    """Complaint classification → escalation regardless of confidence score."""
    scorer_result = ScoringResult(
        provider=ScoringProvider(provider_name="claude", score=0.95, reasoning=""),
        classification=IssueType.complaint,
        response_text=None,
    )
    service, github = _make_advocate_service(
        issues=[_make_issue(title="This is terrible, I hate your service")],
        scorer_result=scorer_result,
    )

    await service.scan_and_respond(set())

    github.add_labels.assert_called()
    applied_label_ids = github.add_labels.call_args[0][1]
    assert "label-escalation" in applied_label_ids


@pytest.mark.asyncio
async def test_low_confidence_escalates_question() -> None:
    """Confidence below threshold → escalates question/confusion."""
    scorer_result = ScoringResult(
        provider=ScoringProvider(provider_name="claude", score=0.40, reasoning="low confidence"),
        classification=IssueType.question,
        response_text=None,
    )
    service, github = _make_advocate_service(
        issues=[_make_issue()],
        scorer_result=scorer_result,
    )

    await service.scan_and_respond(set())

    github.add_labels.assert_called()
    applied_label_ids = github.add_labels.call_args[0][1]
    assert "label-escalation" in applied_label_ids


# ---------------------------------------------------------------------------
# T022 — Documentation loading (US3)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unreachable_doc_source_escalates_with_no_doc_configured() -> None:
    """All sources unreachable → question/confusion issues escalate with no_documentation_configured."""
    scorer_result = ScoringResult(
        provider=ScoringProvider(provider_name="claude", score=0.85, reasoning=""),
        classification=IssueType.question,
        response_text=None,
    )
    service, github = _make_advocate_service(
        issues=[_make_issue()],
        file_content=None,  # unreachable doc
        scorer_result=scorer_result,
    )

    await service.scan_and_respond(set())

    # Should escalate (apply escalation label)
    github.add_labels.assert_called()
    applied_label_ids = github.add_labels.call_args[0][1]
    assert "label-escalation" in applied_label_ids


@pytest.mark.asyncio
async def test_scorer_returns_null_response_text_escalates_with_no_doc_match() -> None:
    """scorer returns response_text=None → escalates with no_documentation_match."""
    scorer_result = ScoringResult(
        provider=ScoringProvider(provider_name="claude", score=0.85, reasoning=""),
        classification=IssueType.question,
        response_text=None,  # no match in docs
    )
    service, github = _make_advocate_service(
        issues=[_make_issue()],
        file_content="# README\nSome docs.",
        scorer_result=scorer_result,
    )

    await service.scan_and_respond(set())

    github.add_labels.assert_called()
    applied_label_ids = github.add_labels.call_args[0][1]
    assert "label-escalation" in applied_label_ids


# ---------------------------------------------------------------------------
# T025 — Triage paths (US4)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_feature_request_acknowledged() -> None:
    """feature_request → advocate-handled label applied, acknowledgement comment posted."""
    scorer_result = ScoringResult(
        provider=ScoringProvider(provider_name="claude", score=0.80, reasoning=""),
        classification=IssueType.feature_request,
        response_text=None,
    )
    service, github = _make_advocate_service(
        issues=[_make_issue(title="Add dark mode")],
        scorer_result=scorer_result,
    )

    await service.scan_and_respond(set())

    github.add_labels.assert_called_with("issue-1", ["label-handled"])
    comment_calls = [c for c in github.method_calls if c[0] == "add_comment"]
    assert len(comment_calls) == 1
    assert "feature request" in comment_calls[0][1][1].lower() or "roadmap" in comment_calls[0][1][1].lower()


@pytest.mark.asyncio
async def test_bug_report_triaged_no_comment() -> None:
    """bug_report → advocate-handled label applied, NO comment posted."""
    scorer_result = ScoringResult(
        provider=ScoringProvider(provider_name="claude", score=0.80, reasoning=""),
        classification=IssueType.bug_report,
        response_text=None,
    )
    service, github = _make_advocate_service(
        issues=[_make_issue(title="App crashes on startup")],
        scorer_result=scorer_result,
    )

    await service.scan_and_respond(set())

    github.add_labels.assert_called_with("issue-1", ["label-handled"])
    github.add_comment.assert_not_called()


@pytest.mark.asyncio
async def test_off_topic_redirect_contains_support_url() -> None:
    """off_topic → advocate-handled label applied, redirect comment with support_channel_url."""
    from coordinare.config import AdvocateConfig
    from coordinare.services.advocate import AdvocateService

    scorer_result = ScoringResult(
        provider=ScoringProvider(provider_name="claude", score=0.80, reasoning=""),
        classification=IssueType.off_topic,
        response_text=None,
    )
    github = AsyncMock()
    github.list_open_issues = AsyncMock(return_value=[_make_issue(title="I want pizza")])
    github.get_file_content = AsyncMock(return_value="# README\nContent.")
    github.add_labels = AsyncMock()
    github.add_comment = AsyncMock()

    config = AdvocateConfig(
        enabled=True,
        github_repo="coordinare",
        support_channel_url="https://example.com/support",
    )
    mock_scorer = MagicMock()
    mock_scorer.score = AsyncMock(return_value=scorer_result)

    service = AdvocateService(
        github=github,
        notification_service=None,
        config=config,
        github_org="org",
        label_ids={"advocate-handled": "lh", "needs-human": "le"},
        scorers=[mock_scorer],
    )

    await service.scan_and_respond(set())

    github.add_labels.assert_called_with("issue-1", ["lh"])
    comment_calls = [c for c in github.method_calls if c[0] == "add_comment"]
    assert len(comment_calls) == 1
    assert "https://example.com/support" in comment_calls[0][1][1]


# ---------------------------------------------------------------------------
# Error handling paths (advocate.py lines 58-60, 92-93)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_open_issues_exception_returns_processed_ids_unchanged() -> None:
    """GitHub list_open_issues raises → scan returns original processed_ids, no crash."""
    service, _ = _make_advocate_service(issues=[])
    service._github.list_open_issues = AsyncMock(side_effect=RuntimeError("network error"))

    original = {"issue-99"}
    result = await service.scan_and_respond(original)

    assert result == original


@pytest.mark.asyncio
async def test_process_one_exception_skips_issue_continues_others() -> None:
    """If _process_issue raises for one issue, that issue is skipped; others are processed."""
    scorer_result = ScoringResult(
        provider=ScoringProvider(provider_name="claude", score=0.85, reasoning=""),
        classification=IssueType.question,
        response_text="Based on `README.md`: answer.",
        source_documents=["README.md"],
    )
    issues = [
        _make_issue(issue_id="issue-1", number=1, title="How do I use X?"),
        _make_issue(issue_id="issue-2", number=2, title="How do I use Y?"),
    ]
    service, _github = _make_advocate_service(issues=issues, scorer_result=scorer_result)

    original_process = service._process_issue
    call_count = {"n": 0}

    async def _patched(issue, doc_sources):
        call_count["n"] += 1
        if issue["id"] == "issue-1":
            raise RuntimeError("transient error")
        return await original_process(issue, doc_sources)

    service._process_issue = _patched  # type: ignore[method-assign]

    updated = await service.scan_and_respond(set())

    # issue-2 was processed despite issue-1 raising
    assert "issue-2" in updated
    assert "issue-1" not in updated


@pytest.mark.asyncio
async def test_all_scorers_fail_escalates_with_api_failure() -> None:
    """All scorers fail (classification=None) → API-failure escalation path."""
    # _make_advocate_service with no scorer_result leaves scorers=[] so
    # compute_consensus returns classification=None (all-failed path).
    service, github = _make_advocate_service(
        issues=[_make_issue(title="How do I configure the poll interval?")],
        scorer_result=None,
    )

    await service.scan_and_respond(set())

    # Escalation label must be applied
    github.add_labels.assert_called()
    applied = github.add_labels.call_args[0][1]
    assert "label-escalation" in applied


@pytest.mark.asyncio
async def test_apply_label_github_failure_logs_and_continues() -> None:
    """add_labels raises → error logged, no crash, processing continues."""
    scorer_result = ScoringResult(
        provider=ScoringProvider(provider_name="claude", score=0.85, reasoning=""),
        classification=IssueType.question,
        response_text="Based on `README.md`: answer.",
        source_documents=["README.md"],
    )
    service, github = _make_advocate_service(
        issues=[_make_issue()],
        scorer_result=scorer_result,
    )
    github.add_labels = AsyncMock(side_effect=RuntimeError("labels API down"))

    # Should not raise
    await service.scan_and_respond(set())


@pytest.mark.asyncio
async def test_post_comment_github_failure_logs_and_continues() -> None:
    """add_comment raises → error logged, no crash, processing continues."""
    scorer_result = ScoringResult(
        provider=ScoringProvider(provider_name="claude", score=0.85, reasoning=""),
        classification=IssueType.question,
        response_text="Based on `README.md`: answer.",
        source_documents=["README.md"],
    )
    service, github = _make_advocate_service(
        issues=[_make_issue()],
        scorer_result=scorer_result,
    )
    github.add_comment = AsyncMock(side_effect=RuntimeError("comments API down"))

    # Should not raise
    await service.scan_and_respond(set())


# ---------------------------------------------------------------------------
# Lines 137-144: doc fetch exception warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_doc_fetch_exception_logs_warning_and_continues() -> None:
    """Lines 137-144: when get_file_content raises, warning is logged and source gets content=None."""
    scorer_result = ScoringResult(
        provider=ScoringProvider(provider_name="claude", score=0.85, reasoning=""),
        classification=IssueType.question,
        response_text="Based on README: here is the answer.",
        source_documents=["README.md"],
    )
    service, github = _make_advocate_service(
        issues=[_make_issue()],
        scorer_result=scorer_result,
    )
    # get_file_content raises — triggers lines 137-144
    github.get_file_content = AsyncMock(side_effect=RuntimeError("network error"))

    # Should not raise
    await service.scan_and_respond(set())


# ---------------------------------------------------------------------------
# Line 265: low confidence → escalate with EscalationReason.low_confidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_low_confidence_question_escalates() -> None:
    """Line 265: confidence below threshold → _do_escalate with low_confidence reason."""
    # confidence=0.5 is below the default threshold of 0.70
    scorer_result = ScoringResult(
        provider=ScoringProvider(provider_name="claude", score=0.5, reasoning=""),
        classification=IssueType.question,
        response_text="Some answer",
        source_documents=["README.md"],
    )
    service, github = _make_advocate_service(
        issues=[_make_issue()],
        scorer_result=scorer_result,
    )

    await service.scan_and_respond(set())

    # Escalation → holding comment posted and escalation label applied
    github.add_comment.assert_called()


# ---------------------------------------------------------------------------
# Line 344->exit: label_id is empty → skip add_labels call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_label_skips_github_when_label_id_is_empty() -> None:
    """Line 344->exit: empty label_id → add_labels NOT called."""
    scorer_result = ScoringResult(
        provider=ScoringProvider(provider_name="claude", score=0.85, reasoning=""),
        classification=IssueType.question,
        response_text="Based on README: answer.",
        source_documents=["README.md"],
    )
    # Provide label_ids with empty string values → label_id is falsy
    service, github = _make_advocate_service(
        issues=[_make_issue()],
        scorer_result=scorer_result,
        label_ids={"advocate-handled": "", "needs-human": ""},
    )

    await service.scan_and_respond(set())

    # add_labels must NOT be called when label_id is empty
    github.add_labels.assert_not_called()


# ---------------------------------------------------------------------------
# Lines 372-398: notification_service dispatched during escalation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalation_dispatches_notification_when_service_set() -> None:
    """Lines 372-398: notification_service.dispatch is called during _do_escalate."""
    from coordinare.config import AdvocateConfig
    from coordinare.services.advocate import AdvocateService

    github = AsyncMock()
    github.list_open_issues = AsyncMock(return_value=[_make_issue()])
    github.get_file_content = AsyncMock(return_value="# Docs\nSome content.")
    github.add_labels = AsyncMock()
    github.add_comment = AsyncMock()

    notification_service = AsyncMock()
    notification_service.dispatch = AsyncMock()

    scorer_result = ScoringResult(
        provider=ScoringProvider(provider_name="claude", score=0.5, reasoning=""),
        classification=IssueType.question,
        response_text="Some answer",
        source_documents=[],
    )
    mock_scorer = MagicMock()
    mock_scorer.score = AsyncMock(return_value=scorer_result)

    config = AdvocateConfig(
        enabled=True,
        github_repo="coordinare",
        confidence_threshold=0.70,
        doc_sources=["README.md"],
    )
    service = AdvocateService(
        github=github,
        notification_service=notification_service,
        config=config,
        github_org="ViviDynamics",
        label_ids={"advocate-handled": "label-handled", "needs-human": "label-escalation"},
        scorers=[mock_scorer],
    )

    await service.scan_and_respond(set())

    # notification_service.dispatch must be called (covers lines 372-398)
    notification_service.dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# Lines 397-398: notification dispatch exception is caught and logged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_escalation_notification_exception_is_caught() -> None:
    """Lines 397-398: if notification_service.dispatch raises, the exception is
    caught and logged — _do_escalate does not propagate it."""
    from coordinare.config import AdvocateConfig
    from coordinare.services.advocate import AdvocateService

    github = AsyncMock()
    github.list_open_issues = AsyncMock(return_value=[_make_issue()])
    github.get_file_content = AsyncMock(return_value="# Docs\nContent.")
    github.add_labels = AsyncMock()
    github.add_comment = AsyncMock()

    notification_service = AsyncMock()
    notification_service.dispatch = AsyncMock(side_effect=RuntimeError("dispatch failed"))

    scorer_result = ScoringResult(
        provider=ScoringProvider(provider_name="claude", score=0.5, reasoning=""),
        classification=IssueType.question,
        response_text="Some answer",
        source_documents=[],
    )
    mock_scorer = MagicMock()
    mock_scorer.score = AsyncMock(return_value=scorer_result)

    config = AdvocateConfig(
        enabled=True,
        github_repo="coordinare",
        confidence_threshold=0.70,
        doc_sources=["README.md"],
    )
    service = AdvocateService(
        github=github,
        notification_service=notification_service,
        config=config,
        github_org="ViviDynamics",
        label_ids={"advocate-handled": "label-handled", "needs-human": "label-escalation"},
        scorers=[mock_scorer],
    )

    # Must NOT raise even though dispatch fails
    await service.scan_and_respond(set())
    notification_service.dispatch.assert_called_once()
