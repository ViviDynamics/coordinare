"""Unit tests for issue comment service (055)."""
from __future__ import annotations

import pytest

from coordinare.services.issue_comment_service import (
    IssueCommentEvent,
    classify_issue_comment,
    classify_issue_comment_ai,
    fetch_new_issue_comments,
)


class TestClassifyIssueComment:
    def test_empty_body_is_noise(self) -> None:
        assert classify_issue_comment("") == "noise"

    def test_whitespace_body_is_noise(self) -> None:
        assert classify_issue_comment("   \n\t  ") == "noise"

    def test_scope_change_keywords(self) -> None:
        assert classify_issue_comment("Can we also add a search feature?") == "scope_change"
        assert classify_issue_comment("Please include logging too") == "scope_change"
        assert classify_issue_comment("We also need authentication") == "scope_change"
        assert classify_issue_comment("Additionally, please log every login attempt") == "scope_change"

    def test_blocker_keywords(self) -> None:
        assert classify_issue_comment("This is blocked by the auth system") == "blocker_update"
        assert classify_issue_comment("Waiting on the database team") == "blocker_update"
        assert classify_issue_comment("This is now unblocked") == "blocker_update"

    def test_approval_keywords(self) -> None:
        assert classify_issue_comment("LGTM") == "approval"
        assert classify_issue_comment("Looks good to me!") == "approval"
        assert classify_issue_comment("Ship it! 👍") == "approval"

    def test_default_to_clarification(self) -> None:
        assert classify_issue_comment("What about edge cases?") == "clarification"
        assert classify_issue_comment("Can you explain the design?") == "clarification"
        assert classify_issue_comment("This is just a comment") == "clarification"

    def test_keyword_not_substring_match(self) -> None:
        # "please add" should match but bare "add" no longer matches; ensure
        # innocuous prose containing "add"/"include" inside other words
        # doesn't trip a scope_change.
        assert classify_issue_comment("Please add a logout button") == "scope_change"
        assert classify_issue_comment("I climbed a ladder") == "clarification"
        assert classify_issue_comment("Run useradd to create the pg user") == "clarification"

    def test_polluted_qa_evidence_post_is_not_scope_change(self) -> None:
        # Regression: the bare-verb "add"/"include"/"extend" keywords used to
        # match QA-bot evidence dumps that pasted bash commands and UI labels
        # like "+ Add Time Entry", causing spurious requirements_changed=True.
        body = (
            "## QA Evidence\n\n**Result:** PASSED\n"
            "1. Click + Add Time Entry and confirm the form only offers the\n"
            "   employee's assigned projects.\n"
            "2. Include the entry in the JSON output for downstream tools.\n"
            "3. Run `useradd -m pguser` to provision the temp account.\n"
        )
        assert classify_issue_comment(body) == "clarification"


class _StubBackend:
    def __init__(self, response):
        self.response = response
        self.calls: list[str] = []

    async def prompt(self, text, response_format=None):
        self.calls.append(text)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class TestClassifyIssueCommentAI:
    @pytest.mark.asyncio
    async def test_none_backend_returns_none(self) -> None:
        assert await classify_issue_comment_ai("anything", "alice", None) is None

    @pytest.mark.asyncio
    async def test_empty_body_short_circuits_to_noise(self) -> None:
        backend = _StubBackend({"data": {"label": "scope_change"}})
        assert await classify_issue_comment_ai("   ", "alice", backend) == "noise"
        assert backend.calls == []  # never invoked

    @pytest.mark.asyncio
    async def test_valid_label_in_data_field(self) -> None:
        backend = _StubBackend({
            "data": {"label": "scope_change", "rationale": "user requested new feature"}
        })
        assert await classify_issue_comment_ai("body", "alice", backend) == "scope_change"

    @pytest.mark.asyncio
    async def test_valid_label_parsed_from_text(self) -> None:
        backend = _StubBackend({
            "data": None,
            "text": '{"label": "clarification", "rationale": "user asked a question"}',
        })
        assert await classify_issue_comment_ai("body", "alice", backend) == "clarification"

    @pytest.mark.asyncio
    async def test_unknown_label_returns_none(self) -> None:
        backend = _StubBackend({"data": {"label": "feature_request"}})
        assert await classify_issue_comment_ai("body", "alice", backend) is None

    @pytest.mark.asyncio
    async def test_backend_exception_returns_none(self) -> None:
        backend = _StubBackend(RuntimeError("boom"))
        assert await classify_issue_comment_ai("body", "alice", backend) is None

    @pytest.mark.asyncio
    async def test_unparseable_text_returns_none(self) -> None:
        backend = _StubBackend({"data": None, "text": "not json at all"})
        assert await classify_issue_comment_ai("body", "alice", backend) is None

    @pytest.mark.asyncio
    async def test_non_dict_result_returns_none(self) -> None:
        backend = _StubBackend("just a string")
        assert await classify_issue_comment_ai("body", "alice", backend) is None


class TestFetchNewIssueComments:
    """153: comments are fetched from the board, addressed by card id.

    These fakes are boards, not GitHub services. That is the change: the function
    no longer knows what a GitHub issue number is, so a board that has none can
    still be read.
    """

    @pytest.mark.asyncio
    async def test_fetch_with_no_card_id_returns_empty(self) -> None:
        class FakeBoard:
            async def get_card_comments(self, card_id, since_id=None):
                raise AssertionError("should not reach the board without a card id")

        assert await fetch_new_issue_comments("", None, FakeBoard()) == []

    @pytest.mark.asyncio
    async def test_fetch_with_no_board_returns_empty(self) -> None:
        """A state with no board at all skips, rather than failing the cycle."""
        assert await fetch_new_issue_comments("card-1", None, None) == []

    @pytest.mark.asyncio
    async def test_a_card_with_no_issue_number_is_still_read(self) -> None:
        """The old gate keyed on the issue number; a Jira card would have routed nothing."""

        class FakeBoard:
            def __init__(self):
                self.asked_for = None

            async def get_card_comments(self, card_id, since_id=None):
                self.asked_for = card_id
                return [{"id": "7", "author": "a", "body": "b", "created_at": "c"}]

        board = FakeBoard()
        result = await fetch_new_issue_comments("PROJ-123", None, board)

        assert board.asked_for == "PROJ-123"
        assert len(result) == 1
        assert result[0].issue_number == 0, "no number is known, and none is invented"

    @pytest.mark.asyncio
    async def test_fetch_new_comments_parses_response(self) -> None:
        class FakeBoard:
            async def get_card_comments(self, card_id, since_id=None):
                return [
                    {
                        "id": "12345",
                        "author": "alice",
                        "body": "This looks good",
                        "created_at": "2025-05-05T10:00:00Z",
                    },
                    {
                        "id": "12346",
                        "author": "bob",
                        "body": "I agree",
                        "created_at": "2025-05-05T10:01:00Z",
                    },
                ]

        result = await fetch_new_issue_comments("card-1", 10000, FakeBoard(), 42)

        assert len(result) == 2
        assert isinstance(result[0], IssueCommentEvent)
        assert result[0].issue_number == 42
        assert result[0].comment_id == 12345
        assert result[0].author == "alice"
        assert result[0].body == "This looks good"
        assert result[0].card_id == "card-1"

        assert result[1].comment_id == 12346
        assert result[1].author == "bob"

    @pytest.mark.asyncio
    async def test_fetch_handles_missing_fields(self) -> None:
        class FakeBoard:
            async def get_card_comments(self, card_id, since_id=None):
                return [
                    {
                        "id": "99999",
                        # author, body, created_at are missing
                    }
                ]

        result = await fetch_new_issue_comments("card-x", None, FakeBoard(), 99)

        assert len(result) == 1
        assert result[0].comment_id == 99999
        assert result[0].author == ""
        assert result[0].body == ""
        assert result[0].created_at == ""

    @pytest.mark.asyncio
    async def test_fetch_exception_returns_empty(self) -> None:
        class FakeBoard:
            async def get_card_comments(self, card_id, since_id=None):
                raise ValueError("board unreachable")

        assert await fetch_new_issue_comments("card-1", 100, FakeBoard(), 42) == []

    @pytest.mark.asyncio
    async def test_fetch_with_since_id(self) -> None:
        class FakeBoard:
            def __init__(self):
                self.last_since_id = None

            async def get_card_comments(self, card_id, since_id=None):
                self.last_since_id = since_id
                return [
                    {
                        "id": "5000",
                        "author": "charlie",
                        "body": "New comment",
                        "created_at": "2025-05-05T11:00:00Z",
                    }
                ]

        board = FakeBoard()
        result = await fetch_new_issue_comments("card-2", 4999, board, 15)

        assert len(result) == 1
        assert board.last_since_id == 4999, "the watermark must survive the move to the board"
