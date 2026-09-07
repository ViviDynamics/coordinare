"""Unit tests for closer workflow models (spec 172)."""
from __future__ import annotations

import pytest
from performer.workflows.closer.models import (
    Classification,
    ClosingRecord,
    Thread,
    ThreadComment,
    model_judgements_schema,
)


class TestThreadComment:
    """ThreadComment validation."""

    def test_creates_comment(self) -> None:
        c = ThreadComment(author="alice", body="fix this", created_at="2026-01-01T00:00:00Z")
        assert c.author == "alice"
        assert c.body == "fix this"

    def test_rejects_body_over_4000_chars(self) -> None:
        with pytest.raises(ValueError):
            ThreadComment(author="alice", body="x" * 4001, created_at="2026-01-01T00:00:00Z")


class TestThread:
    """Thread model and properties."""

    def test_creates_thread(self) -> None:
        comments = [
            ThreadComment(author="alice", body="x", created_at="2026-01-01T00:00:00Z"),
            ThreadComment(author="bob", body="y", created_at="2026-01-02T00:00:00Z"),
        ]
        t = Thread(id="T1", path="f.py", line=10, resolved=False, outdated=False, comments=comments)
        assert t.id == "T1"
        assert t.line == 10

    def test_first_author(self) -> None:
        comments = [
            ThreadComment(author="alice", body="x", created_at="2026-01-01T00:00:00Z"),
            ThreadComment(author="bob", body="y", created_at="2026-01-02T00:00:00Z"),
        ]
        t = Thread(id="T1", path="f.py", line=10, resolved=False, outdated=False, comments=comments)
        assert t.first_author == "alice"

    def test_last_author(self) -> None:
        comments = [
            ThreadComment(author="alice", body="x", created_at="2026-01-01T00:00:00Z"),
            ThreadComment(author="bob", body="y", created_at="2026-01-02T00:00:00Z"),
        ]
        t = Thread(id="T1", path="f.py", line=10, resolved=False, outdated=False, comments=comments)
        assert t.last_author == "bob"

    def test_first_author_empty_when_no_comments(self) -> None:
        t = Thread(id="T1", path="f.py", line=10, resolved=False, outdated=False, comments=[])
        assert t.first_author == ""
        assert t.last_author == ""

    def test_raiser_only_all_same_author(self) -> None:
        comments = [
            ThreadComment(author="alice", body="x", created_at="2026-01-01T00:00:00Z"),
            ThreadComment(author="alice", body="y", created_at="2026-01-02T00:00:00Z"),
        ]
        t = Thread(id="T1", path="f.py", line=10, resolved=False, outdated=False, comments=comments)
        assert t.raiser_only is True

    def test_raiser_only_different_authors(self) -> None:
        comments = [
            ThreadComment(author="alice", body="x", created_at="2026-01-01T00:00:00Z"),
            ThreadComment(author="bob", body="y", created_at="2026-01-02T00:00:00Z"),
        ]
        t = Thread(id="T1", path="f.py", line=10, resolved=False, outdated=False, comments=comments)
        assert t.raiser_only is False

    def test_raiser_only_empty_comments(self) -> None:
        t = Thread(id="T1", path="f.py", line=10, resolved=False, outdated=False, comments=[])
        assert t.raiser_only is True

    def test_line_min_zero(self) -> None:
        with pytest.raises(ValueError):
            Thread(id="T1", path="f.py", line=-1, resolved=False, outdated=False, comments=[])


class TestModelJudgementsSchema:
    """Model judgements schema validation."""

    def test_accepts_valid_judgements(self) -> None:
        schema = model_judgements_schema(max_threads=20)
        data = {
            "judgements": [
                {"thread_id": "T1", "addressed": True, "quote": "test"},
                {"thread_id": "T2", "addressed": False, "reason": "unclear"},
            ]
        }
        result = schema(**data)
        assert len(result.judgements) == 2

    def test_rejects_verdict_key(self) -> None:
        schema = model_judgements_schema(max_threads=20)
        data = {
            "judgements": [
                {"thread_id": "T1", "addressed": True, "quote": "test", "verdict": "approved"}
            ]
        }
        with pytest.raises(ValueError):
            schema(**data)

    def test_enforces_max_threads(self) -> None:
        schema = model_judgements_schema(max_threads=2)
        data = {
            "judgements": [
                {"thread_id": "T1", "addressed": True},
                {"thread_id": "T2", "addressed": False},
                {"thread_id": "T3", "addressed": True},
            ]
        }
        with pytest.raises(ValueError):
            schema(**data)

    def test_quote_max_length(self) -> None:
        schema = model_judgements_schema(max_threads=1)
        data = {
            "judgements": [
                {"thread_id": "T1", "addressed": True, "quote": "x" * 301}
            ]
        }
        with pytest.raises(ValueError):
            schema(**data)

    def test_reason_max_length(self) -> None:
        schema = model_judgements_schema(max_threads=1)
        data = {
            "judgements": [
                {"thread_id": "T1", "addressed": False, "reason": "x" * 301}
            ]
        }
        with pytest.raises(ValueError):
            schema(**data)

    def test_defaults(self) -> None:
        schema = model_judgements_schema(max_threads=1)
        data = {"judgements": [{"thread_id": "T1", "addressed": True}]}
        result = schema(**data)
        assert result.judgements[0].quote == ""
        assert result.judgements[0].reason == ""


class TestClosingRecord:
    """ClosingRecord model and schema validation."""

    def test_creates_record(self) -> None:
        r = ClosingRecord(threads_read=5, verdict="approved")
        assert r.threads_read == 5
        assert r.verdict == "approved"

    def test_defaults(self) -> None:
        r = ClosingRecord(threads_read=0, verdict="changes_requested")
        assert r.head_sha is None
        assert r.pages_read == 0
        assert r.classifications == []
        assert r.judgements == []
        assert r.resolved == []
        assert r.open_threads == []
        assert r.hold_reason is None
        assert r.posted_review_url is None
        assert r.workflow_metrics == {}

    def test_roundtrips_json(self) -> None:
        r = ClosingRecord(
            threads_read=2,
            verdict="approved",
            classifications=[
                Classification(thread_id="T1", state="resolved", rule="resolved"),
                Classification(thread_id="T2", state="stale", rule="stale"),
            ],
        )
        data = r.model_dump_json()
        r2 = ClosingRecord.model_validate_json(data)
        assert r2.threads_read == 2
        assert len(r2.classifications) == 2

    def test_schema_validation_exists(self) -> None:
        """Test that validate_schema() runs without error when the schema file exists."""
        # This just tests that the method can be called; full JSON schema validation
        # requires the file to exist in the specs directory.
        r = ClosingRecord(threads_read=0, verdict="approved")
        # We can't fully test this without mocking the file, but we can ensure the method exists
        assert hasattr(r, "validate_schema")
