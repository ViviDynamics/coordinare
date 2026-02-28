"""SC-006 performance test — advocate scan processes 20 issues within 10 s."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.config import AdvocateConfig
from coordinare.models.advocate import IssueType, ScoringProvider
from coordinare.services.advocate import AdvocateService
from coordinare.services.scoring import ScoringResult


def _make_issues(n: int) -> list[dict]:
    return [
        {
            "id": f"issue-{i}",
            "number": i,
            "title": f"How do I use feature {i}?",
            "body": "I need help.",
            "url": f"https://github.com/org/repo/issues/{i}",
            "labels": {"nodes": []},
        }
        for i in range(1, n + 1)
    ]


@pytest.mark.asyncio
async def test_advocate_scan_20_issues_within_10_seconds() -> None:
    """SC-006: processing 20 issues must complete in ≤ 10 s of wall time.

    All external I/O (GitHub API, Claude API) is mocked with coroutines that
    return instantly, so this test validates concurrency overhead only, not
    network latency. Any regression that introduces blocking calls or
    sequential issue processing will break this budget.
    """
    issues = _make_issues(20)

    github = AsyncMock()
    github.list_open_issues = AsyncMock(return_value=issues)
    github.get_file_content = AsyncMock(return_value="# README\nDocumentation here.")
    github.add_labels = AsyncMock()
    github.add_comment = AsyncMock()

    scorer_result = ScoringResult(
        provider=ScoringProvider(provider_name="claude", score=0.90, reasoning="clear"),
        classification=IssueType.question,
        response_text="Based on `README.md`: the answer is here.",
        source_documents=["README.md"],
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
        notification_service=None,
        config=config,
        github_org="org",
        label_ids={"advocate-handled": "lh", "needs-human": "le"},
        scorers=[mock_scorer],
    )

    start = time.monotonic()
    await service.scan_and_respond(set())
    elapsed = time.monotonic() - start

    assert elapsed < 10.0, (
        f"SC-006 violated: advocate scan for 20 issues took {elapsed:.2f}s (budget: 10s)"
    )
    # All 20 issues must have been acted on
    assert github.add_labels.call_count == 20
    assert github.add_comment.call_count == 20
