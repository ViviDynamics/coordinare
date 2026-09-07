"""Unit tests for fetch_review_threads and resolve_review_threads in performer.github."""
from __future__ import annotations

import pytest
import respx
import httpx

from performer.github import (
    GitHubAPIError,
    fetch_review_threads,
    resolve_review_threads,
    resolve_pr_review_threads,
)


class TestFetchReviewThreads:
    """Fetch threads with full comment details and pagination support."""

    _GRAPHQL = "https://api.github.com/graphql"

    def _thread_response(self, threads: list[dict], has_next: bool = False, end_cursor: str | None = None) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
                                "nodes": threads,
                            }
                        }
                    }
                }
            },
        )

    @respx.mock
    async def test_fetches_single_page(self) -> None:
        threads = [
            {
                "id": "T1",
                "isResolved": False,
                "isOutdated": False,
                "path": "file.py",
                "line": 10,
                "comments": {
                    "nodes": [
                        {
                            "author": {"login": "alice"},
                            "body": "Add docstring",
                            "createdAt": "2026-01-01T00:00:00Z",
                        },
                        {
                            "author": {"login": "bob"},
                            "body": "Done",
                            "createdAt": "2026-01-02T00:00:00Z",
                        },
                    ]
                },
            }
        ]
        respx.post(self._GRAPHQL).mock(return_value=self._thread_response(threads))

        fetched, pages = await fetch_review_threads("org", "repo", 42, "tok")

        assert pages == 1
        assert len(fetched) == 1
        assert fetched[0]["id"] == "T1"
        assert fetched[0]["path"] == "file.py"
        assert fetched[0]["line"] == 10
        assert fetched[0]["resolved"] is False
        assert fetched[0]["outdated"] is False
        assert len(fetched[0]["comments"]) == 2
        assert fetched[0]["comments"][0]["author"] == "alice"
        assert fetched[0]["comments"][1]["body"] == "Done"

    @respx.mock
    async def test_paging_over_two_pages(self) -> None:
        """Fetch paging with hasNextPage and cursor."""
        import json as _json

        page1_threads = [{"id": "T1", "isResolved": False, "isOutdated": False, "path": "a.py", "line": 1, "comments": {"nodes": []}}]
        page2_threads = [{"id": "T2", "isResolved": False, "isOutdated": True, "path": "b.py", "line": 2, "comments": {"nodes": []}}]

        def _route(request: httpx.Request) -> httpx.Response:
            body = request.read().decode()
            payload = _json.loads(body)
            cursor = payload.get("variables", {}).get("cursor")
            if cursor is None:
                return self._thread_response(page1_threads, has_next=True, end_cursor="cursor_2")
            return self._thread_response(page2_threads, has_next=False, end_cursor=None)

        respx.post(self._GRAPHQL).mock(side_effect=_route)

        fetched, pages = await fetch_review_threads("org", "repo", 42, "tok", max_pages=5)

        assert pages == 2
        assert len(fetched) == 2
        assert fetched[0]["id"] == "T1"
        assert fetched[1]["id"] == "T2"

    @respx.mock
    async def test_null_author_returns_empty_string(self) -> None:
        """Deleted users have null author, should become empty string."""
        threads = [
            {
                "id": "T1",
                "isResolved": False,
                "isOutdated": False,
                "path": "f.py",
                "line": 5,
                "comments": {
                    "nodes": [
                        {
                            "author": None,
                            "body": "This user is deleted",
                            "createdAt": "2026-01-01T00:00:00Z",
                        }
                    ]
                },
            }
        ]
        respx.post(self._GRAPHQL).mock(return_value=self._thread_response(threads))

        fetched, _pages = await fetch_review_threads("org", "repo", 42, "tok")

        assert fetched[0]["comments"][0]["author"] == ""

    @respx.mock
    async def test_null_line_becomes_zero(self) -> None:
        """Comments without a line (e.g., file-level) have null line."""
        threads = [
            {
                "id": "T1",
                "isResolved": False,
                "isOutdated": False,
                "path": "f.py",
                "line": None,
                "comments": {"nodes": []},
            }
        ]
        respx.post(self._GRAPHQL).mock(return_value=self._thread_response(threads))

        fetched, _pages = await fetch_review_threads("org", "repo", 42, "tok")

        assert fetched[0]["line"] == 0

    @respx.mock
    async def test_graphql_errors_raise(self) -> None:
        """GraphQL errors in the response raise GitHubAPIError."""
        respx.post(self._GRAPHQL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": None,
                    "errors": [{"message": "rate limited"}],
                },
            )
        )

        with pytest.raises(GitHubAPIError):
            await fetch_review_threads("org", "repo", 42, "tok")

    @respx.mock
    async def test_http_error_raises(self) -> None:
        """Non-success HTTP response raises GitHubAPIError."""
        respx.post(self._GRAPHQL).mock(return_value=httpx.Response(500, text="Internal Server Error"))

        with pytest.raises(GitHubAPIError) as exc_info:
            await fetch_review_threads("org", "repo", 42, "tok")
        assert exc_info.value.status_code == 500

    async def test_empty_token_raises(self) -> None:
        with pytest.raises(GitHubAPIError) as exc_info:
            await fetch_review_threads("org", "repo", 42, "")
        assert exc_info.value.status_code == 401


class TestResolveReviewThreads:
    """Resolve specific thread IDs, returning resolved ids and failures."""

    _GRAPHQL = "https://api.github.com/graphql"

    def _mutation_success(self, thread_id: str) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "resolveReviewThread": {
                        "thread": {"id": thread_id, "isResolved": True}
                    }
                }
            },
        )

    def _mutation_failure(self, message: str) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {"resolveReviewThread": None},
                "errors": [{"message": message}],
            },
        )

    @respx.mock
    async def test_resolves_all_threads(self) -> None:
        """Resolve a list of thread IDs successfully."""
        def _route(request: httpx.Request) -> httpx.Response:
            import json as _json
            payload = _json.loads(request.read().decode())
            tid = payload["variables"]["id"]
            return self._mutation_success(tid)

        respx.post(self._GRAPHQL).mock(side_effect=_route)

        resolved, failures = await resolve_review_threads("org", "repo", ["T1", "T2"], "tok")

        assert len(resolved) == 2
        assert "T1" in resolved
        assert "T2" in resolved
        assert len(failures) == 0

    @respx.mock
    async def test_partial_failures(self) -> None:
        """Some resolve, some fail; failures are captured."""
        def _route(request: httpx.Request) -> httpx.Response:
            import json as _json
            payload = _json.loads(request.read().decode())
            tid = payload["variables"]["id"]
            if tid == "T_fail":
                return self._mutation_failure("permission denied")
            return self._mutation_success(tid)

        respx.post(self._GRAPHQL).mock(side_effect=_route)

        resolved, failures = await resolve_review_threads("org", "repo", ["T_ok", "T_fail"], "tok")

        assert len(resolved) == 1
        assert "T_ok" in resolved
        assert len(failures) == 1
        assert failures[0]["id"] == "T_fail"
        assert "errors" in failures[0]

    @respx.mock
    async def test_http_error_captured(self) -> None:
        """HTTP errors are captured in the failures list."""
        respx.post(self._GRAPHQL).mock(return_value=httpx.Response(500, text="Server error"))

        resolved, failures = await resolve_review_threads("org", "repo", ["T1"], "tok")

        assert len(resolved) == 0
        assert len(failures) == 1
        assert failures[0]["id"] == "T1"
        assert "status" in failures[0]

    async def test_empty_token_raises(self) -> None:
        with pytest.raises(GitHubAPIError) as exc_info:
            await resolve_review_threads("org", "repo", ["T1"], "")
        assert exc_info.value.status_code == 401


class TestResolvePrReviewThreadsRefactored:
    """Regression tests: resolve_pr_review_threads still works and uses the new helpers."""

    _GRAPHQL = "https://api.github.com/graphql"

    def _thread_response(self, threads: list[dict]) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": threads,
                            }
                        }
                    }
                }
            },
        )

    def _mutation_success(self, thread_id: str) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "resolveReviewThread": {
                        "thread": {"id": thread_id, "isResolved": True}
                    }
                }
            },
        )

    @respx.mock
    async def test_resolves_and_returns_count(self) -> None:
        """resolve_pr_review_threads returns the count of resolved threads."""
        threads = [
            {
                "id": "T1",
                "isResolved": False,
                "isOutdated": False,
                "path": "a.py",
                "line": 1,
                "comments": {
                    "nodes": [{"author": {"login": "alice"}, "body": "x", "createdAt": "2026-01-01T00:00:00Z"}]
                },
            },
            {
                "id": "T2",
                "isResolved": True,
                "isOutdated": False,
                "path": "b.py",
                "line": 2,
                "comments": {"nodes": []},
            },
        ]

        def _route(request: httpx.Request) -> httpx.Response:
            body = request.read().decode()
            if "reviewThreads" in body:
                return self._thread_response(threads)
            import json as _json
            payload = _json.loads(body)
            tid = payload["variables"]["id"]
            return self._mutation_success(tid)

        respx.post(self._GRAPHQL).mock(side_effect=_route)

        resolved = await resolve_pr_review_threads("org", "repo", 42, "tok")

        # Only T1 is unresolved
        assert resolved == 1

    @respx.mock
    async def test_returns_zero_on_fetch_error(self) -> None:
        """Fetch errors return 0 (matching old behavior)."""
        respx.post(self._GRAPHQL).mock(return_value=httpx.Response(500, text="error"))

        resolved = await resolve_pr_review_threads("org", "repo", 42, "tok")

        assert resolved == 0

    @respx.mock
    async def test_returns_zero_when_all_resolved(self) -> None:
        """When all threads are already resolved, return 0."""
        threads = [
            {
                "id": "T1",
                "isResolved": True,
                "isOutdated": False,
                "path": "a.py",
                "line": 1,
                "comments": {"nodes": []},
            }
        ]
        respx.post(self._GRAPHQL).mock(return_value=self._thread_response(threads))

        resolved = await resolve_pr_review_threads("org", "repo", 42, "tok")

        assert resolved == 0


# --- spec 172 review: comment pagination beyond the first 100 ---------------------

@pytest.mark.asyncio
async def test_a_thread_with_more_than_100_comments_pages_the_rest(monkeypatch):
    """Review finding: comments(first: 100) truncated silently, so the closer's
    last-comment rule read the wrong comment."""
    import httpx

    from performer import github as gh

    thread_page = {"data": {"repository": {"pullRequest": {"reviewThreads": {
        "pageInfo": {"hasNextPage": False, "endCursor": None},
        "nodes": [{"id": "t1", "path": "src/a.py", "line": 3, "isResolved": False, "isOutdated": False,
                   "comments": {"pageInfo": {"hasNextPage": True, "endCursor": "c100"},
                                "nodes": [{"author": {"login": "reviewer"}, "body": "first", "createdAt": "2026-01-01T00:00:00Z"}]}}]}}}}}
    comment_page = {"data": {"node": {"comments": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                                   "nodes": [{"author": {"login": "implementer"}, "body": "last", "createdAt": "2026-01-02T00:00:00Z"}]}}}}
    calls = {"n": 0}

    class _Resp:
        def __init__(self, payload):
            self._payload = payload
            self.is_success = True
            self.status_code = 200

        def json(self):
            return self._payload

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            calls["n"] += 1
            return _Resp(thread_page if calls["n"] == 1 else comment_page)

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client())
    threads, pages = await gh.fetch_review_threads("o", "r", 1, "tok")
    assert calls["n"] == 2, "one thread page plus one comment page"
    assert [c["body"] for c in threads[0]["comments"]] == ["first", "last"]
    assert threads[0]["comments"][-1]["author"] == "implementer"
