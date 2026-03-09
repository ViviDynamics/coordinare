"""Unit tests for performer.models — constitution Principle II."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from performer.models import Performance, Score, Stand


class TestScore:
    def test_valid_github_url(self) -> None:
        s = Score(
            title="T",
            repo_url="https://github.com/org/repo",
            branch="main",
            github_token="ghp_abc123",
        )
        assert s.repo_url == "https://github.com/org/repo"

    def test_valid_github_url_with_dotgit(self) -> None:
        s = Score(
            title="T",
            repo_url="https://github.com/org/repo.git",
            branch="main",
            github_token="tok",
        )
        assert s.repo_url == "https://github.com/org/repo.git"

    def test_invalid_repo_url_not_github(self) -> None:
        with pytest.raises(ValidationError, match="repo_url"):
            Score(
                title="T",
                repo_url="https://gitlab.com/org/repo",
                branch="main",
                github_token="tok",
            )

    def test_invalid_repo_url_http(self) -> None:
        with pytest.raises(ValidationError, match="repo_url"):
            Score(
                title="T",
                repo_url="http://github.com/org/repo",
                branch="main",
                github_token="tok",
            )

    def test_blank_github_token_raises(self) -> None:
        with pytest.raises(ValidationError, match="github_token"):
            Score(
                title="T",
                repo_url="https://github.com/org/repo",
                branch="main",
                github_token="   ",
            )

    def test_empty_github_token_raises(self) -> None:
        with pytest.raises(ValidationError, match="github_token"):
            Score(
                title="T",
                repo_url="https://github.com/org/repo",
                branch="main",
                github_token="",
            )

    def test_optional_fields_default(self) -> None:
        s = Score(
            title="T",
            repo_url="https://github.com/org/repo",
            branch="feat/x",
            github_token="tok",
        )
        assert s.description == ""
        assert s.acceptance_criteria == []
        assert s.base_branch == ""


class TestStand:
    def test_holds_fields(self) -> None:
        now = datetime.now(UTC)
        stand = Stand(path=Path("/tmp/performer-abc"), branch="feat/x", created_at=now)
        assert stand.path == Path("/tmp/performer-abc")
        assert stand.branch == "feat/x"
        assert stand.created_at == now

    def test_created_at_defaults_to_now(self) -> None:
        before = datetime.now(UTC)
        stand = Stand(path=Path("/tmp/x"), branch="main")
        after = datetime.now(UTC)
        assert before <= stand.created_at <= after


class TestPerformance:
    def _make_score(self) -> Score:
        return Score(
            title="T",
            repo_url="https://github.com/org/repo",
            branch="main",
            github_token="tok",
        )

    def _make_stand(self) -> Stand:
        return Stand(path=Path("/tmp/x"), branch="main")

    def test_initial_state_is_accepted(self) -> None:
        perf = Performance(
            session_id="sid",
            stand=self._make_stand(),
            score=self._make_score(),
            backend=MagicMock(),
        )
        assert perf.state == "accepted"

    def test_fields_accessible(self) -> None:
        perf = Performance(
            session_id="sid-123",
            stand=self._make_stand(),
            score=self._make_score(),
            backend=MagicMock(),
        )
        assert perf.session_id == "sid-123"
        assert perf.pr_url is None
        assert perf.pr_node_id is None
        assert perf.open_questions == []
        assert perf.error_reason is None
