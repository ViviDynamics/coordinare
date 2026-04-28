"""Phase 7 integration tests for 055 — QA visual testing + comment orchestration.

Tests span multiple modules end-to-end with mocked external I/O:
  1. QA node with mocked Docker + CDN → PR comment markdown with embedded screenshots
  2. Issue comment → detected next cycle → classified → requirements_changed=True
  3. Same issue comment processed twice → only dispatched once (idempotency)
  4. Doc dedup pass on workspace with overlapping spec.md + requirements.md
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coordinare.graph.nodes.qa_screenshots import qa_screenshots
from coordinare.graph.nodes.route_issue_comments import route_issue_comments
from coordinare.graph.state import initial_state
from coordinare.services.cdn_upload import render_screenshot_section
from coordinare.services.screenshot_service import DockerSession, QAScreenshotResult
from coordinare.utils.doc_dedup import DocDeduplicationResult, merge_duplicate_sections

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state(**overrides):
    s = initial_state()
    s.update(overrides)
    return s


class _FakeGitHub:
    """Minimal GitHub stub; add attributes as tests require."""

    def __init__(self, comments=None):
        self._comments = comments or []
        self._token = "tok"
        self._org = "acme"
        self._project_name = "repo"

    async def get_issue_comments(self, issue_number, *, since_id=None):
        return self._comments


# ---------------------------------------------------------------------------
# Test 1 — QA node: Docker + CDN → screenshot markdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qa_node_mocked_docker_and_cdn_produces_screenshot_results(tmp_path):
    """Full QA node flow: Docker launches, screenshots captured, CDN upload succeeds."""
    screenshot_file = tmp_path / "qa_screenshots" / "home.png"
    screenshot_file.parent.mkdir(parents=True)
    screenshot_file.write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal PNG header

    mock_session = DockerSession(
        container_id="c1",
        app_url="http://localhost:3000",
        screenshot_dir=tmp_path / "qa_screenshots",
    )
    fake_results = [
        QAScreenshotResult(
            feature_area="home",
            file_path=screenshot_file,
            cdn_url=None,
            status="ok",
        )
    ]

    state = _state(
        current_card={"id": "CARD_1", "issue_number": 42},
        workspace_path=tmp_path,
    )

    cdn_url = "https://github.com/user-attachments/assets/home.png"

    with (
        patch(
            "coordinare.graph.nodes.qa_screenshots.launch_docker_env",
            new_callable=AsyncMock,
            return_value=mock_session,
        ),
        patch(
            "coordinare.graph.nodes.qa_screenshots.capture_screenshots",
            new_callable=AsyncMock,
            return_value=fake_results,
        ),
        patch(
            "coordinare.graph.nodes.qa_screenshots.teardown_docker_env",
            new_callable=AsyncMock,
        ),
        patch(
            "coordinare.graph.nodes.qa_screenshots.upload_screenshot",
            new_callable=AsyncMock,
            return_value=cdn_url,
        ),
    ):
        # Wire a github service with the required attributes
        github = MagicMock()
        github.current_token = AsyncMock(return_value="tok")
        github.org = "acme"
        github.project_name = "repo"
        state["github_service"] = github

        result = await qa_screenshots(state)

    screenshots = result["qa_screenshots"]
    assert len(screenshots) == 1
    assert screenshots[0].feature_area == "home"
    assert screenshots[0].cdn_url == cdn_url
    assert screenshots[0].status == "ok"

    # Verify render produces correct markdown
    md = render_screenshot_section([
        {"feature_area": s.feature_area, "cdn_url": s.cdn_url, "status": s.status}
        for s in screenshots
    ])
    assert "## Screenshots" in md
    assert cdn_url in md
    assert "![home" in md


# ---------------------------------------------------------------------------
# Test 2 — Issue comment → classified → requirements_changed=True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_issue_comment_scope_change_sets_requirements_changed():
    """A scope-change comment on the linked issue sets requirements_changed=True."""
    github = _FakeGitHub(comments=[
        {
            "id": 101,
            "author": "alice",
            "body": "Please also add dark mode support to this feature",
            "created_at": "2026-04-27T10:00:00Z",
        }
    ])

    state = _state(
        github_service=github,
        current_card={"id": "CARD_1", "issue_number": 7},
        requirements_changed=False,
    )

    result = await route_issue_comments(state)

    assert result["requirements_changed"] is True
    clarifications = result["card_clarifications"]
    assert len(clarifications) == 1
    assert clarifications[0]["classification"] == "scope_change"
    assert clarifications[0]["source"] == "issue"
    assert clarifications[0]["comment_id"] == 101


# ---------------------------------------------------------------------------
# Test 3 — Idempotency: same comment processed twice → only once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_issue_comment_idempotency_processes_only_once():
    """Running route_issue_comments twice on the same comment doesn't duplicate clarifications."""
    comment = {
        "id": 202,
        "author": "bob",
        "body": "Can you please add pagination to the table?",
        "created_at": "2026-04-27T11:00:00Z",
    }
    github = _FakeGitHub(comments=[comment])

    state = _state(
        github_service=github,
        current_card={"id": "CARD_2", "issue_number": 9},
        requirements_changed=False,
        processed_issue_comment_ids=set(),
    )

    # First pass
    result = await route_issue_comments(state)
    assert len(result["card_clarifications"]) == 1
    assert 202 in result["processed_issue_comment_ids"]

    # Second pass — same comment is still returned by the mock github service
    result2 = await route_issue_comments(result)
    assert len(result2["card_clarifications"]) == 1  # not doubled


# ---------------------------------------------------------------------------
# Test 4 — Doc dedup pass on workspace with overlapping .md files
# ---------------------------------------------------------------------------


def test_doc_dedup_merges_spec_and_requirements_overlap(tmp_path):
    """Overlapping sections between spec.md and requirements.md are merged into spec.md (canonical)."""
    shared_overview = (
        "# Overview\n\n"
        "This feature adds QA screenshot capture to the performer pipeline.\n"
        "Screenshots are uploaded to GitHub CDN and embedded in PR comments.\n"
    )
    spec_content = shared_overview + "# Architecture\n\nDetails here.\n"
    req_content = shared_overview + "# Acceptance Criteria\n\n- [ ] Screenshots captured\n"

    (tmp_path / "spec.md").write_text(spec_content)
    (tmp_path / "requirements.md").write_text(req_content)

    result: DocDeduplicationResult = merge_duplicate_sections(
        tmp_path, canonical_priority=["spec.md"]
    )

    assert result.sections_merged >= 1
    # spec.md (canonical) keeps the Overview section
    assert "# Overview" in (tmp_path / "spec.md").read_text()
    # requirements.md (non-canonical) has Overview removed
    req_text = (tmp_path / "requirements.md").read_text()
    assert "# Overview" not in req_text
    # Non-Overview sections are preserved in their respective files
    assert "# Acceptance Criteria" in req_text
    assert "# Architecture" in (tmp_path / "spec.md").read_text()
