"""Unit tests for closer workflow classification rules (spec 172).

Each test mutates a rule to verify it is essential. Mutations are named
in the test docstring so they fail when fixed.
"""
from __future__ import annotations

from performer.workflows.closer.classify import (
    classify_thread,
    is_answered,
    is_resolved,
    is_stale,
)
from performer.workflows.closer.models import Thread, ThreadComment


class TestIsResolved:
    """Mutation: ignore the resolved flag."""

    def test_resolved_thread_is_resolved(self) -> None:
        t = Thread(id="T1", path="f.py", line=1, resolved=True, outdated=False, comments=[])
        assert is_resolved(t) is True

    def test_unresolved_thread_is_not_resolved(self) -> None:
        t = Thread(id="T1", path="f.py", line=1, resolved=False, outdated=False, comments=[])
        assert is_resolved(t) is False


class TestIsStale:
    """Mutation: drop the resolved clause (treat resolved-and-outdated as stale)."""

    def test_stale_thread_is_stale(self) -> None:
        t = Thread(id="T1", path="f.py", line=1, resolved=False, outdated=True, comments=[])
        assert is_stale(t) is True

    def test_resolved_and_outdated_is_not_stale(self) -> None:
        """A thread marked resolved by GitHub is not stale, even if outdated."""
        t = Thread(id="T1", path="f.py", line=1, resolved=True, outdated=True, comments=[])
        assert is_stale(t) is False

    def test_unresolved_but_not_outdated_is_not_stale(self) -> None:
        t = Thread(id="T1", path="f.py", line=1, resolved=False, outdated=False, comments=[])
        assert is_stale(t) is False


class TestIsAnswered:
    """Mutation: drop the author comparison (raiser's follow-up counts as answered)."""

    def test_answered_thread_is_answered(self) -> None:
        """Last comment by different author, later timestamp."""
        comments = [
            ThreadComment(author="alice", body="needs fix", created_at="2026-01-01T00:00:00Z"),
            ThreadComment(author="bob", body="done", created_at="2026-01-02T00:00:00Z"),
        ]
        t = Thread(id="T1", path="f.py", line=1, resolved=False, outdated=False, comments=comments)
        assert is_answered(t) is True

    def test_raiser_own_followup_reaches_the_judge(self) -> None:
        """A raiser commenting after a reply re-opens the thread for judgement (spec 414):
        classify only decides ambiguity, and the gate owns quote authority."""
        comments = [
            ThreadComment(author="alice", body="needs fix", created_at="2026-01-01T00:00:00Z"),
            ThreadComment(author="bob", body="done", created_at="2026-01-02T00:00:00Z"),
            ThreadComment(author="alice", body="thanks", created_at="2026-01-03T00:00:00Z"),
        ]
        t = Thread(id="T1", path="f.py", line=1, resolved=False, outdated=False, comments=comments)
        assert is_answered(t) is True

    def test_raiser_double_comment_without_a_reply_stays_open(self) -> None:
        """Two raiser comments with no reply in between: nobody answered."""
        comments = [
            ThreadComment(author="alice", body="needs fix", created_at="2026-01-01T00:00:00Z"),
            ThreadComment(author="alice", body="to be clear: it raises IndexError", created_at="2026-01-03T00:00:00Z"),
        ]
        t = Thread(id="T1", path="f.py", line=1, resolved=False, outdated=False, comments=comments)
        assert is_answered(t) is False

    def test_single_comment_is_not_answered(self) -> None:
        """Single comment cannot be answered."""
        comments = [
            ThreadComment(author="alice", body="needs fix", created_at="2026-01-01T00:00:00Z"),
        ]
        t = Thread(id="T1", path="f.py", line=1, resolved=False, outdated=False, comments=comments)
        assert is_answered(t) is False

    def test_equal_timestamps_counts_as_answered(self) -> None:
        """When timestamps are equal, >= allows it."""
        comments = [
            ThreadComment(author="alice", body="needs fix", created_at="2026-01-01T00:00:00Z"),
            ThreadComment(author="bob", body="done", created_at="2026-01-01T00:00:00Z"),
        ]
        t = Thread(id="T1", path="f.py", line=1, resolved=False, outdated=False, comments=comments)
        assert is_answered(t) is True

    def test_outdated_is_not_answered_even_with_reply(self) -> None:
        """Mutation: drop the outdated clause. Outdated threads cannot be answered by code."""
        comments = [
            ThreadComment(author="alice", body="needs fix", created_at="2026-01-01T00:00:00Z"),
            ThreadComment(author="bob", body="done", created_at="2026-01-02T00:00:00Z"),
        ]
        t = Thread(id="T1", path="f.py", line=1, resolved=False, outdated=True, comments=comments)
        assert is_answered(t) is False

    def test_resolved_is_not_answered(self) -> None:
        """Already resolved threads are not answered (resolved rule comes first)."""
        comments = [
            ThreadComment(author="alice", body="needs fix", created_at="2026-01-01T00:00:00Z"),
            ThreadComment(author="bob", body="done", created_at="2026-01-02T00:00:00Z"),
        ]
        t = Thread(id="T1", path="f.py", line=1, resolved=True, outdated=False, comments=comments)
        assert is_answered(t) is False


class TestClassifyThread:
    """Mutation: check answered before stale (order matters)."""

    def test_resolved_is_resolved(self) -> None:
        t = Thread(id="T1", path="f.py", line=1, resolved=True, outdated=False, comments=[])
        c = classify_thread(t)
        assert c.state == "resolved"
        assert c.rule == "resolved"

    def test_stale_is_stale(self) -> None:
        t = Thread(id="T1", path="f.py", line=1, resolved=False, outdated=True, comments=[])
        c = classify_thread(t)
        assert c.state == "stale"
        assert c.rule == "stale"

    def test_answered_is_answered(self) -> None:
        comments = [
            ThreadComment(author="alice", body="x", created_at="2026-01-01T00:00:00Z"),
            ThreadComment(author="bob", body="y", created_at="2026-01-02T00:00:00Z"),
        ]
        t = Thread(id="T1", path="f.py", line=1, resolved=False, outdated=False, comments=comments)
        c = classify_thread(t)
        assert c.state == "answered"
        assert c.rule == "answered"

    def test_open_is_open(self) -> None:
        comments = [
            ThreadComment(author="alice", body="x", created_at="2026-01-01T00:00:00Z"),
        ]
        t = Thread(id="T1", path="f.py", line=1, resolved=False, outdated=False, comments=comments)
        c = classify_thread(t)
        assert c.state == "open"
        assert c.rule == "open"

    def test_thread_id_preserved(self) -> None:
        t = Thread(id="my_thread", path="f.py", line=1, resolved=True, outdated=False, comments=[])
        c = classify_thread(t)
        assert c.thread_id == "my_thread"
