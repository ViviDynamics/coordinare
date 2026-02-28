"""Integration tests for the full advocate scan cycle (007, T017/T021)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.config import AdvocateConfig
from coordinare.models.advocate import IssueType, ScoringProvider
from coordinare.services.advocate import AdvocateService
from coordinare.services.scoring import ScoringResult
from tests.utils.fake_notification import FakeNotificationService


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


def _make_service(
    issues: list[dict],
    scorer_result: ScoringResult,
    notification_service: FakeNotificationService | None = None,
    file_content: str | None = "# README\nX works like this.",
    github_repo: str = "coordinare",
) -> tuple[AdvocateService, AsyncMock]:
    github = AsyncMock()
    github.list_open_issues = AsyncMock(return_value=issues)
    github.get_file_content = AsyncMock(return_value=file_content)
    github.add_labels = AsyncMock()
    github.add_comment = AsyncMock()

    config = AdvocateConfig(
        enabled=True,
        github_repo=github_repo,
        confidence_threshold=0.70,
        doc_sources=["README.md"],
        support_channel_url="https://example.com/support",
    )

    mock_scorer = MagicMock()
    mock_scorer.score = AsyncMock(return_value=scorer_result)

    service = AdvocateService(
        github=github,
        notification_service=notification_service,
        config=config,
        github_org="org",
        label_ids={"advocate-handled": "lh", "needs-human": "le"},
        scorers=[mock_scorer],
    )
    return service, github


# ---------------------------------------------------------------------------
# US1: Auto-reply to questions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_question_full_cycle_label_comment_dedup() -> None:
    """Issue fetched → label applied before comment → comment contains citation and disclosure → processed_ids updated."""
    scorer_result = ScoringResult(
        provider=ScoringProvider(provider_name="claude", score=0.85, reasoning=""),
        classification=IssueType.question,
        response_text="Based on `README.md`: X works like this.",
        source_documents=["README.md"],
    )
    service, github = _make_service([_make_issue()], scorer_result)

    updated_ids = await service.scan_and_respond(set())

    # Label applied
    github.add_labels.assert_called_once_with("issue-1", ["lh"])
    # Comment posted
    comment_calls = [c for c in github.method_calls if c[0] == "add_comment"]
    assert len(comment_calls) == 1
    comment_body = comment_calls[0][1][1]
    assert "README.md" in comment_body
    assert service._config.disclosure_template in comment_body
    # processed_ids updated
    assert "issue-1" in updated_ids


@pytest.mark.asyncio
async def test_second_cycle_same_issue_no_duplicate_comment() -> None:
    """Second cycle on same issue → no duplicate comment."""
    scorer_result = ScoringResult(
        provider=ScoringProvider(provider_name="claude", score=0.85, reasoning=""),
        classification=IssueType.question,
        response_text="Based on `README.md`: answer.",
        source_documents=["README.md"],
    )
    service, github = _make_service([_make_issue()], scorer_result)

    # First cycle
    processed = await service.scan_and_respond(set())
    first_comment_count = github.add_comment.call_count

    # Second cycle — same issue is in processed_ids
    await service.scan_and_respond(processed)

    # No additional comments
    assert github.add_comment.call_count == first_comment_count


# ---------------------------------------------------------------------------
# US2: Human escalation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sensitive_keyword_escalation_no_ai_answer() -> None:
    """Sensitive keyword issue → needs-human label + holding comment + notification sent + no AI answer."""
    fake_notifications = FakeNotificationService()
    issue = _make_issue(title="billing question about my account")
    scorer_result = ScoringResult(
        provider=ScoringProvider(provider_name="claude", score=0.9, reasoning=""),
        classification=IssueType.question,
        response_text="answer",
    )
    service, svc_github = _make_service(
        [issue], scorer_result, notification_service=fake_notifications
    )
    # Scorer should not be called
    service._scorers[0].score = AsyncMock()

    await service.scan_and_respond(set())

    # needs-human label applied
    svc_github.add_labels.assert_called_with("issue-1", ["le"])
    # Holding comment posted
    comment_calls = [c for c in svc_github.method_calls if c[0] == "add_comment"]
    assert len(comment_calls) == 1
    assert "team member" in comment_calls[0][1][1].lower() or "follow up" in comment_calls[0][1][1].lower()
    # Notification dispatched
    assert len(fake_notifications.dispatched) >= 1
    # Scorer not called
    service._scorers[0].score.assert_not_called()


@pytest.mark.asyncio
async def test_low_confidence_escalation_notification_contains_reason() -> None:
    """Low-confidence mock → notification payload contains issue URL and escalation reason."""
    from coordinare.models.notification import EventType

    fake_notifications = FakeNotificationService()
    scorer_result = ScoringResult(
        provider=ScoringProvider(provider_name="claude", score=0.40, reasoning=""),
        classification=IssueType.question,
        # response_text is non-None so the flow reaches the confidence check
        # (None would escalate as no_documentation_match before checking confidence)
        response_text="Based on `README.md`: a tentative answer.",
    )
    service, _ = _make_service(
        [_make_issue()], scorer_result, notification_service=fake_notifications
    )

    await service.scan_and_respond(set())

    assert len(fake_notifications.dispatched) >= 1
    event = fake_notifications.dispatched[0]
    assert event.event_type == EventType.advocate_escalation
    assert event.payload.get("reason") == "low_confidence"
    assert event.payload.get("issue_number") == "1"
