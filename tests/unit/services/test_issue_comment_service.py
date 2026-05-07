"""Unit tests for issue comment service (055)."""
from __future__ import annotations

import pytest

from coordinare.services.issue_comment_service import (
    IssueCommentEvent,
    classify_issue_comment,
    fetch_new_issue_comments,
)


class TestClassifyIssueComment:
    def test_empty_body_is_noise(self) -> None:
        assert classify_issue_comment("") == "noise"

    def test_whitespace_body_is_noise(self) -> None:
        assert classify_issue_comment("   \n\t  ") == "noise"

    def test_scope_change_keywords(self) -> None:
        assert classify_issue_comment("Can we also add a search feature?") == "scope_change"
        assert classify_issue_comment("Please include logging") == "scope_change"
        assert classify_issue_comment("We should also need authentication") == "scope_change"

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
        # "add" should match but "ladder" shouldn't
        assert classify_issue_comment("We should add a feature") == "scope_change"
        assert classify_issue_comment("I climbed a ladder") == "clarification"


class TestFetchNewIssueComments:
    @pytest.mark.asyncio
    async def test_fetch_with_zero_issue_number_returns_empty(self) -> None:
        class FakeGithubService:
            async def get_issue_comments(self, issue_number, since_id=None):
                raise AssertionError("should not call github")

        service = FakeGithubService()
        result = await fetch_new_issue_comments(0, None, service, "card-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_fetch_with_none_issue_number_returns_empty(self) -> None:
        class FakeGithubService:
            async def get_issue_comments(self, issue_number, since_id=None):
                raise AssertionError("should not call github")

        service = FakeGithubService()
        result = await fetch_new_issue_comments(None, None, service, "card-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_fetch_new_comments_parses_response(self) -> None:
        class FakeGithubService:
            async def get_issue_comments(self, issue_number, since_id=None):
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

        service = FakeGithubService()
        result = await fetch_new_issue_comments(42, 10000, service, "card-1")

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
        class FakeGithubService:
            async def get_issue_comments(self, issue_number, since_id=None):
                return [
                    {
                        "id": "99999",
                        # author, body, created_at are missing
                    }
                ]

        service = FakeGithubService()
        result = await fetch_new_issue_comments(99, None, service, "card-x")

        assert len(result) == 1
        assert result[0].comment_id == 99999
        assert result[0].author == ""
        assert result[0].body == ""
        assert result[0].created_at == ""

    @pytest.mark.asyncio
    async def test_fetch_exception_returns_empty(self) -> None:
        class FakeGithubService:
            async def get_issue_comments(self, issue_number, since_id=None):
                raise ValueError("GitHub API error")

        service = FakeGithubService()
        result = await fetch_new_issue_comments(42, 100, service, "card-1")
        assert result == []

    @pytest.mark.asyncio
    async def test_fetch_with_since_id(self) -> None:
        class FakeGithubService:
            def __init__(self):
                self.last_since_id = None

            async def get_issue_comments(self, issue_number, since_id=None):
                self.last_since_id = since_id
                return [
                    {
                        "id": "5000",
                        "author": "charlie",
                        "body": "New comment",
                        "created_at": "2025-05-05T11:00:00Z",
                    }
                ]

        service = FakeGithubService()
        result = await fetch_new_issue_comments(15, 4999, service, "card-2")

        assert len(result) == 1
        assert service.last_since_id == 4999
