"""Unit tests for performer.models — constitution Principle II."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from performer.models import BackendEvent, BackendEventType, Performance, Score, Stand, _redact_secrets


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

    def test_invalid_repo_url_no_path(self) -> None:
        """URL with no owner/repo path is rejected."""
        with pytest.raises(ValidationError, match="repo_url"):
            Score(
                title="T",
                repo_url="https://github.com",
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

    def test_empty_github_token_allowed_for_kubernetes(self) -> None:
        """Empty github_token is valid — Kubernetes transport injects auth via container secrets."""
        s = Score(
            title="T",
            repo_url="https://github.com/org/repo",
            branch="main",
            github_token="",
        )
        assert s.github_token == ""

    def test_blank_github_token_allowed_for_kubernetes(self) -> None:
        s = Score(
            title="T",
            repo_url="https://github.com/org/repo",
            branch="main",
            github_token="   ",
        )
        assert s.github_token == "   "

    def test_blank_branch_raises(self) -> None:
        with pytest.raises(ValidationError, match="branch"):
            Score(
                title="T",
                repo_url="https://github.com/org/repo",
                branch="   ",
                github_token="tok",
            )

    def test_empty_branch_raises(self) -> None:
        with pytest.raises(ValidationError, match="branch"):
            Score(
                title="T",
                repo_url="https://github.com/org/repo",
                branch="",
                github_token="tok",
            )

    def test_branch_with_space_raises(self) -> None:
        with pytest.raises(ValidationError, match="branch"):
            Score(
                title="T",
                repo_url="https://github.com/org/repo",
                branch="feat ure",
                github_token="tok",
            )

    def test_branch_with_dotdot_raises(self) -> None:
        with pytest.raises(ValidationError, match="branch"):
            Score(
                title="T",
                repo_url="https://github.com/org/repo",
                branch="feat..x",
                github_token="tok",
            )

    def test_effective_github_token_returns_payload_token_when_set(self) -> None:
        s = Score(
            title="T", repo_url="https://github.com/org/repo", branch="main",
            github_token="ghp_from_payload",
        )
        assert s.effective_github_token == "ghp_from_payload"

    def test_effective_github_token_falls_back_to_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_from_env")
        s = Score(title="T", repo_url="https://github.com/org/repo", branch="main", github_token="")
        assert s.effective_github_token == "ghp_from_env"

    def test_effective_github_token_empty_when_both_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        s = Score(title="T", repo_url="https://github.com/org/repo", branch="main", github_token="")
        assert s.effective_github_token == ""

    def test_effective_github_token_strips_whitespace_payload(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Whitespace-only payload token falls back to env (treated as missing)."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_from_env")
        s = Score(title="T", repo_url="https://github.com/org/repo", branch="main", github_token="   ")
        assert s.effective_github_token == "ghp_from_env"

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


class TestRedactSecrets:
    """T003 — unit tests for _redact_secrets()."""

    def test_github_fine_grained_pat(self) -> None:
        token = "github_pat_" + "A" * 82
        result = _redact_secrets(f"token={token}")
        assert "[REDACTED]" in result
        assert token not in result

    def test_github_classic_pat(self) -> None:
        token = "ghp_" + "A" * 36
        assert _redact_secrets(token) == "[REDACTED]"

    def test_github_oauth_token(self) -> None:
        token = "gho_" + "B" * 36
        assert _redact_secrets(token) == "[REDACTED]"

    def test_github_app_installation_token(self) -> None:
        token = "ghs_" + "C" * 36
        assert _redact_secrets(token) == "[REDACTED]"

    def test_anthropic_api_key(self) -> None:
        key = "sk-ant-" + "x" * 90
        assert _redact_secrets(key) == "[REDACTED]"

    def test_bearer_token(self) -> None:
        header = "Bearer " + "a" * 20
        assert _redact_secrets(header) == "[REDACTED]"

    def test_aws_access_key(self) -> None:
        key = "AKIA" + "A" * 16
        assert _redact_secrets(key) == "[REDACTED]"

    def test_non_matching_string_passes_through(self) -> None:
        text = "Read file src/app/main.py"
        assert _redact_secrets(text) == text

    def test_empty_string(self) -> None:
        assert _redact_secrets("") == ""

    def test_idempotent(self) -> None:
        token = "ghp_" + "Z" * 36
        once = _redact_secrets(token)
        twice = _redact_secrets(once)
        assert once == twice == "[REDACTED]"

    def test_already_redacted_not_double_processed(self) -> None:
        assert _redact_secrets("[REDACTED]") == "[REDACTED]"

    def test_multiple_secrets_in_same_string(self) -> None:
        token1 = "ghp_" + "A" * 36
        token2 = "AKIA" + "B" * 16
        text = f"foo {token1} bar {token2} baz"
        result = _redact_secrets(text)
        assert token1 not in result
        assert token2 not in result
        assert result.count("[REDACTED]") == 2


class TestBackendEventRedaction:
    """T004 — unit tests for BackendEvent model_validator redaction."""

    def _make_event(self, detail: str, text: str = "some text") -> BackendEvent:
        return BackendEvent(type=BackendEventType.tool_use, text=text, detail=detail)

    def test_classic_pat_in_detail_is_redacted(self) -> None:
        token = "ghp_" + "A" * 36
        event = self._make_event(detail=token)
        assert event.detail == "[REDACTED]"

    def test_github_fine_grained_pat_in_detail_is_redacted(self) -> None:
        token = "github_pat_" + "B" * 82
        event = self._make_event(detail=f"Authorization: {token}")
        assert token not in event.detail
        assert "[REDACTED]" in event.detail

    def test_github_oauth_token_in_detail_is_redacted(self) -> None:
        token = "gho_" + "C" * 36
        event = self._make_event(detail=token)
        assert event.detail == "[REDACTED]"

    def test_github_app_token_in_detail_is_redacted(self) -> None:
        token = "ghs_" + "D" * 36
        event = self._make_event(detail=token)
        assert event.detail == "[REDACTED]"

    def test_anthropic_key_in_detail_is_redacted(self) -> None:
        key = "sk-ant-" + "e" * 90
        event = self._make_event(detail=key)
        assert event.detail == "[REDACTED]"

    def test_bearer_token_in_detail_is_redacted(self) -> None:
        header = "Bearer " + "f" * 20
        event = self._make_event(detail=header)
        assert event.detail == "[REDACTED]"

    def test_aws_key_in_detail_is_redacted(self) -> None:
        key = "AKIA" + "G" * 16
        event = self._make_event(detail=key)
        assert event.detail == "[REDACTED]"

    def test_text_field_is_not_redacted(self) -> None:
        token = "ghp_" + "H" * 36
        event = BackendEvent(
            type=BackendEventType.progress,
            text=token,
            detail="normal detail",
        )
        assert event.text == token  # text is NOT redacted
        assert event.detail == "normal detail"

    def test_empty_detail_unchanged(self) -> None:
        event = self._make_event(detail="")
        assert event.detail == ""

    def test_non_secret_detail_unchanged(self) -> None:
        event = self._make_event(detail="Edit: src/app/main.py")
        assert event.detail == "Edit: src/app/main.py"

    def test_detail_defaults_to_empty_string(self) -> None:
        event = BackendEvent(type=BackendEventType.progress, text="hello")
        assert event.detail == ""


# ---------------------------------------------------------------------------
# 020 — Architect performer model additions
# ---------------------------------------------------------------------------


class TestPlanCommittedStatus:
    """Tests for plan_committed status and Performance fields (020)."""

    def test_plan_committed_is_valid_performance_state(self) -> None:
        from performer.models import Performance, Stand
        from pathlib import Path
        from unittest.mock import MagicMock
        from performer.models import Score

        stand = Stand(path=Path("/tmp/test"), branch="feat/test")
        score = Score(
            title="Test", repo_url="https://github.com/acme/repo", branch="feat/test",
        )
        perf = Performance(
            session_id="s1", stand=stand, score=score, backend=MagicMock(),
        )
        perf.state = "plan_committed"
        assert perf.state == "plan_committed"

    def test_performance_defaults_for_role_and_plan_path(self) -> None:
        from performer.models import Performance, Stand
        from pathlib import Path
        from unittest.mock import MagicMock
        from performer.models import Score

        stand = Stand(path=Path("/tmp/test"), branch="feat/test")
        score = Score(
            title="Test", repo_url="https://github.com/acme/repo", branch="feat/test",
        )
        perf = Performance(
            session_id="s1", stand=stand, score=score, backend=MagicMock(),
        )
        assert perf.role == "implementing"
        assert perf.plan_path is None

    def test_performance_role_can_be_set_to_architecting(self) -> None:
        from performer.models import Performance, Stand
        from pathlib import Path
        from unittest.mock import MagicMock
        from performer.models import Score

        stand = Stand(path=Path("/tmp/test"), branch="feat/test")
        score = Score(
            title="Test", repo_url="https://github.com/acme/repo", branch="feat/test",
        )
        perf = Performance(
            session_id="s1", stand=stand, score=score, backend=MagicMock(),
            role="architecting",
        )
        assert perf.role == "architecting"


class TestReviewerModelFields:
    """Tests for reviewer-related model additions (021)."""

    def test_approved_is_valid_performance_state(self) -> None:
        from performer.models import Performance, Stand, Score
        from pathlib import Path
        from unittest.mock import MagicMock

        stand = Stand(path=Path("/tmp/test"), branch="feat/test")
        score = Score(title="Test", repo_url="https://github.com/acme/repo", branch="feat/test")
        perf = Performance(session_id="s1", stand=stand, score=score, backend=MagicMock())
        perf.state = "approved"
        assert perf.state == "approved"

    def test_changes_requested_is_valid_performance_state(self) -> None:
        from performer.models import Performance, Stand, Score
        from pathlib import Path
        from unittest.mock import MagicMock

        stand = Stand(path=Path("/tmp/test"), branch="feat/test")
        score = Score(title="Test", repo_url="https://github.com/acme/repo", branch="feat/test")
        perf = Performance(session_id="s1", stand=stand, score=score, backend=MagicMock())
        perf.state = "changes_requested"
        assert perf.state == "changes_requested"

    def test_review_comments_defaults_to_empty_list(self) -> None:
        from performer.models import Performance, Stand, Score
        from pathlib import Path
        from unittest.mock import MagicMock

        stand = Stand(path=Path("/tmp/test"), branch="feat/test")
        score = Score(title="Test", repo_url="https://github.com/acme/repo", branch="feat/test")
        perf = Performance(session_id="s1", stand=stand, score=score, backend=MagicMock())
        assert perf.review_comments == []

    def test_review_cycle_defaults_to_zero(self) -> None:
        from performer.models import Performance, Stand, Score
        from pathlib import Path
        from unittest.mock import MagicMock

        stand = Stand(path=Path("/tmp/test"), branch="feat/test")
        score = Score(title="Test", repo_url="https://github.com/acme/repo", branch="feat/test")
        perf = Performance(session_id="s1", stand=stand, score=score, backend=MagicMock())
        assert perf.review_cycle == 0
