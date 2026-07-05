from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from coordinare.services.github import (
    FIND_PROJECT_QUERY,
    GET_PROJECT_FIELDS_QUERY,
    GitHubService,
    PermanentGitHubError,
    RateLimitedGitHubError,
    TransientGitHubError,
)


class _FakeClient:
    def __init__(self, responses: list[dict[str, Any] | str], *, async_mode: bool) -> None:
        self._responses = responses
        self._async_mode = async_mode
        self.calls: list[tuple[object, dict[str, Any]]] = []

    async def _execute_async(self, query: object, variable_values: dict[str, Any]) -> Any:
        self.calls.append((query, variable_values))
        return self._responses.pop(0)

    def execute(self, query: object, variable_values: dict[str, Any]) -> Any:
        if self._async_mode:
            return self._execute_async(query, variable_values)
        self.calls.append((query, variable_values))
        return self._responses.pop(0)


class _TestGitHubService(GitHubService):
    def __init__(self, client: _FakeClient) -> None:
        super().__init__(token="tok", org="acme", project_number=1)
        self._fake_client = client

    def _build_client(self, token: str = ""):
        return self._fake_client


@pytest.mark.asyncio
async def test_execute_handles_async_client() -> None:
    client = _FakeClient([{"data": {"ok": True}}], async_mode=True)
    service = _TestGitHubService(client)

    result = await service._execute("query X { __typename }", {"x": 1})

    assert result == {"data": {"ok": True}}
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_execute_handles_sync_client() -> None:
    client = _FakeClient([{"data": {"ok": True}}], async_mode=False)
    service = _TestGitHubService(client)

    result = await service._execute("query X { __typename }", {"x": 1})

    assert result == {"data": {"ok": True}}


@pytest.mark.asyncio
async def test_execute_rejects_non_object_response() -> None:
    client = _FakeClient(["bad-response"], async_mode=False)
    service = _TestGitHubService(client)

    with pytest.raises(PermanentGitHubError, match="JSON object"):
        await service._execute("query X { __typename }", {"x": 1})


@pytest.mark.asyncio
async def test_initialize_populates_project_and_field_cache() -> None:
    client = _FakeClient(
        [
            {"organization": {"projectV2": {"id": "PVT_1", "title": "Board"}}},
            {
                "node": {
                    "fields": {
                        "nodes": [
                            {
                                "id": "FLD_1",
                                "name": "Status",
                                "options": [
                                    {"id": "OPT_1", "name": "Todo"},
                                    {"id": "OPT_2", "name": "In Progress"},
                                ],
                            }
                        ]
                    }
                }
            },
        ],
        async_mode=True,
    )
    service = _TestGitHubService(client)

    await service.initialize()

    assert service.project_id == "PVT_1"
    assert service.project_title == "Board"
    assert service.field_cache["status_field_id"] == "FLD_1"
    assert service.field_cache["status_option_ids"]["todo"] == "OPT_1"
    assert service.field_cache["status_option_ids"]["in progress"] == "OPT_2"


class _RateLimitClient:
    """Fake client that raises aiohttp.ClientResponseError with 429."""

    def __init__(self, retry_after: str = "5") -> None:
        self._retry_after = retry_after

    def execute(self, query: object, variable_values: dict[str, Any]) -> Any:
        return self._execute_async(query, variable_values)

    async def _execute_async(self, query: object, variable_values: dict[str, Any]) -> Any:
        headers = {"Retry-After": self._retry_after}
        raise aiohttp.ClientResponseError(
            request_info=aiohttp.RequestInfo(
                url="https://api.github.com/graphql",
                method="POST",
                headers={},
                real_url="https://api.github.com/graphql",
            ),
            history=(),
            status=429,
            headers=headers,
        )


class _ServerErrorClient:
    """Fake client that raises aiohttp.ClientResponseError with 500."""

    def execute(self, query: object, variable_values: dict[str, Any]) -> Any:
        return self._execute_async(query, variable_values)

    async def _execute_async(self, query: object, variable_values: dict[str, Any]) -> Any:
        raise aiohttp.ClientResponseError(
            request_info=aiohttp.RequestInfo(
                url="https://api.github.com/graphql",
                method="POST",
                headers={},
                real_url="https://api.github.com/graphql",
            ),
            history=(),
            status=500,
        )


@pytest.mark.asyncio
async def test_execute_429_raises_rate_limited_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-005: HTTP 429 with Retry-After header -> RateLimitedGitHubError."""
    # Patch asyncio.sleep to avoid real delay
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("coordinare.services.github.asyncio.sleep", fake_sleep)

    service = GitHubService(token="tok", org="acme", project_number=1)
    service._client = _RateLimitClient(retry_after="5")
    service._last_token = "tok"  # prevent _execute from rebuilding the client

    with pytest.raises(RateLimitedGitHubError) as exc_info:
        await service._execute("query { __typename }", {})

    assert exc_info.value.retry_after == 5.0
    assert slept == [5.0]


@pytest.mark.asyncio
async def test_execute_500_raises_transient_error() -> None:
    """HTTP 500 -> TransientGitHubError (not PermanentGitHubError)."""
    service = GitHubService(token="tok", org="acme", project_number=1)
    service._client = _ServerErrorClient()
    service._last_token = "tok"  # prevent _execute from rebuilding the client

    with pytest.raises(TransientGitHubError):
        await service._execute("query { __typename }", {})


def test_query_constants_include_expected_operation_names() -> None:
    assert "FindProject" in FIND_PROJECT_QUERY
    assert "GetProjectFields" in GET_PROJECT_FIELDS_QUERY


# ---------------------------------------------------------------------------
# 036 — GitHub Enterprise Support: custom endpoint wiring
# ---------------------------------------------------------------------------


class TestGitHubServiceCustomEndpoint:
    """Verify GitHubService respects the endpoint parameter for GHES support."""

    def test_default_endpoint(self) -> None:
        service = GitHubService(token="tok", org="acme", project_number=1)
        assert service._endpoint == "https://api.github.com/graphql"

    def test_custom_endpoint(self) -> None:
        service = GitHubService(
            token="tok", org="acme", project_number=1,
            endpoint="https://github.acme.corp/api/graphql",
        )
        assert service._endpoint == "https://github.acme.corp/api/graphql"

    def test_transport_url_matches_custom_endpoint(self) -> None:
        """The AIOHTTPTransport URL should match the configured endpoint."""
        service = GitHubService(
            token="tok", org="acme", project_number=1,
            endpoint="https://ghes.example.com/api/graphql",
        )
        client = service._build_client("test-token")
        transport = client.transport
        assert transport.url == "https://ghes.example.com/api/graphql"


# ---------------------------------------------------------------------------
# T014 — branch_exists and delete_branch (052 stale branch cleanup)
# ---------------------------------------------------------------------------


def _make_branch_service(endpoint: str = "https://api.github.com/graphql") -> GitHubService:
    svc = GitHubService(token="tok", org="acme", project_number=1, endpoint=endpoint)
    svc._project_name = "myrepo"
    return svc


class TestBranchExists:
    @pytest.mark.asyncio
    async def test_returns_true_on_200(self) -> None:
        svc = _make_branch_service()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await svc.branch_exists("coordinare/CARD-89/add-auth")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_on_404(self) -> None:
        svc = _make_branch_service()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await svc.branch_exists("coordinare/CARD-99/no-such-branch")
        assert result is False

    @pytest.mark.asyncio
    async def test_rest_base_standard(self) -> None:
        svc = _make_branch_service("https://api.github.com/graphql")
        assert svc._rest_api_base() == "https://api.github.com"

    @pytest.mark.asyncio
    async def test_rest_base_ghe(self) -> None:
        svc = _make_branch_service("https://github.corp.com/api/graphql")
        assert svc._rest_api_base() == "https://github.corp.com/api/v3"


class TestDeleteBranch:
    @pytest.mark.asyncio
    async def test_delete_204_success(self) -> None:
        svc = _make_branch_service()
        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.delete = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=mock_client):
            await svc.delete_branch("coordinare/CARD-89/add-auth")
        mock_client.delete.assert_called_once()

    @pytest.mark.asyncio
    async def test_delete_422_logs_warning_does_not_raise(self) -> None:
        from structlog.testing import capture_logs

        svc = _make_branch_service()
        mock_resp = MagicMock()
        mock_resp.is_success = False
        mock_resp.status_code = 422
        mock_resp.text = "Unprocessable Entity"
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.delete = AsyncMock(return_value=mock_resp)
        with capture_logs() as cap, patch("httpx.AsyncClient", return_value=mock_client):
            await svc.delete_branch("coordinare/CARD-89/add-auth")
        events = [e.get("event") for e in cap]
        assert "workspace.stale_branch_delete_failed" in events


class TestBranchHasOpenPr:
    @pytest.mark.asyncio
    async def test_returns_true_when_open_pr_exists(self) -> None:
        svc = _make_branch_service()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.json.return_value = [{"number": 123}]
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await svc.branch_has_open_pr("coordinare/CARD-89/add-auth")
        assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_no_open_pr(self) -> None:
        svc = _make_branch_service()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.is_success = True
        mock_resp.json.return_value = []
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await svc.branch_has_open_pr("coordinare/CARD-89/add-auth")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_none_on_404_repo_inaccessible_or_missing(self) -> None:
        svc = _make_branch_service()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.is_success = False
        mock_resp.text = "Not Found"
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await svc.branch_has_open_pr("coordinare/CARD-89/add-auth")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_unexpected_status(self) -> None:
        svc = _make_branch_service()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.is_success = False
        mock_resp.text = "server error"
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await svc.branch_has_open_pr("coordinare/CARD-89/add-auth")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_request_failure(self) -> None:
        svc = _make_branch_service()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=RuntimeError("network down"))
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await svc.branch_has_open_pr("coordinare/CARD-89/add-auth")
        assert result is None


# ---------------------------------------------------------------------------
# 074 — get_pr_files outage/truncation contract
# ---------------------------------------------------------------------------


def _mock_resp(status: int, body: Any) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.is_success = 200 <= status < 300
    r.json = MagicMock(return_value=body)
    return r


class TestGetPrFiles:
    @pytest.mark.asyncio
    async def test_invalid_pr_number_sets_error(self) -> None:
        svc = _make_branch_service()
        result = await svc.get_pr_files("acme", "repo", 0)
        assert result["error"] == "invalid_pr_number"
        assert result["files"] == []
        assert result["truncated"] is False

    @pytest.mark.asyncio
    async def test_pr_fetch_failure_sets_error(self) -> None:
        svc = _make_branch_service()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=_mock_resp(503, {}))
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await svc.get_pr_files("acme", "repo", 42)
        assert result["error"] == "pr_fetch_status:503"
        assert result["files"] == []

    @pytest.mark.asyncio
    async def test_request_exception_sets_error(self) -> None:
        svc = _make_branch_service()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await svc.get_pr_files("acme", "repo", 42)
        assert result["error"].startswith("request_failed:RuntimeError")

    @pytest.mark.asyncio
    async def test_empty_pr_is_not_an_error(self) -> None:
        svc = _make_branch_service()
        pr_resp = _mock_resp(200, {"head": {"sha": "deadbeef"}})
        files_resp = _mock_resp(200, [])
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=[pr_resp, files_resp])
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await svc.get_pr_files("acme", "repo", 42)
        assert result["error"] is None
        assert result["files"] == []
        assert result["head_sha"] == "deadbeef"
        assert result["truncated"] is False

    @pytest.mark.asyncio
    async def test_truncated_when_three_full_pages(self) -> None:
        svc = _make_branch_service()
        pr_resp = _mock_resp(200, {"head": {"sha": "abc"}})
        full_page = [
            {"filename": f"f{i}.py", "additions": 1, "deletions": 0, "status": "modified"}
            for i in range(100)
        ]
        files_p1 = _mock_resp(200, full_page)
        files_p2 = _mock_resp(200, full_page)
        files_p3 = _mock_resp(200, full_page)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=[pr_resp, files_p1, files_p2, files_p3])
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await svc.get_pr_files("acme", "repo", 42)
        assert result["error"] is None
        assert result["truncated"] is True
        assert len(result["files"]) == 300

    @pytest.mark.asyncio
    async def test_not_truncated_when_last_page_partial(self) -> None:
        svc = _make_branch_service()
        pr_resp = _mock_resp(200, {"head": {"sha": "abc"}})
        partial = [
            {"filename": "a.py", "additions": 1, "deletions": 0, "status": "modified"},
        ]
        files_p1 = _mock_resp(200, partial)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=[pr_resp, files_p1])
        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await svc.get_pr_files("acme", "repo", 42)
        assert result["truncated"] is False
        assert len(result["files"]) == 1


# ---------------------------------------------------------------------------
# 083 — get_pr_diff (Contract 2): raw unified diff + changed-file list
# ---------------------------------------------------------------------------


def _diff_resp(status: int, text: str) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.is_success = 200 <= status < 300
    r.text = text
    return r


_SAMPLE_DIFF = """diff --git a/src/app/vuln.py b/src/app/vuln.py
index 1111111..2222222 100644
--- a/src/app/vuln.py
+++ b/src/app/vuln.py
@@ -1,3 +1,5 @@
 import os
+def run(cmd):
+    os.system(cmd)
diff --git a/docs/readme.md b/docs/readme.md
index 3333333..4444444 100644
--- a/docs/readme.md
+++ b/docs/readme.md
@@ -1 +1,2 @@
 hello
+world
"""

_PR_URL = "https://github.com/acme/myrepo/pull/42"


class TestGetPrDiff:
    @pytest.mark.asyncio
    async def test_success_returns_diff_and_changed_files(self) -> None:
        svc = _make_branch_service()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=_diff_resp(200, _SAMPLE_DIFF))
        with patch("httpx.AsyncClient", return_value=mock_client):
            raw, files = await svc.get_pr_diff(_PR_URL)
        assert raw == _SAMPLE_DIFF
        assert files == ["src/app/vuln.py", "docs/readme.md"]

    @pytest.mark.asyncio
    async def test_empty_diff_returns_empty_file_list(self) -> None:
        svc = _make_branch_service()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=_diff_resp(200, ""))
        with patch("httpx.AsyncClient", return_value=mock_client):
            raw, files = await svc.get_pr_diff(_PR_URL)
        assert raw == ""
        assert files == []

    @pytest.mark.asyncio
    async def test_fetch_failure_raises(self) -> None:
        svc = _make_branch_service()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=_diff_resp(503, ""))
        with patch("httpx.AsyncClient", return_value=mock_client), pytest.raises(RuntimeError):
            await svc.get_pr_diff(_PR_URL)

    @pytest.mark.asyncio
    async def test_request_exception_raises(self) -> None:
        svc = _make_branch_service()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=RuntimeError("boom"))
        with patch("httpx.AsyncClient", return_value=mock_client), pytest.raises(RuntimeError):
            await svc.get_pr_diff(_PR_URL)

    @pytest.mark.asyncio
    async def test_malformed_pr_url_raises(self) -> None:
        svc = _make_branch_service()
        with pytest.raises(ValueError):
            await svc.get_pr_diff("https://example.com/not/a/pr")

    @pytest.mark.asyncio
    async def test_token_and_raw_diff_not_logged_at_info(self) -> None:
        from structlog.testing import capture_logs

        svc = _make_branch_service()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=_diff_resp(200, _SAMPLE_DIFF))
        with (
            capture_logs() as cap,
            patch("httpx.AsyncClient", return_value=mock_client),
        ):
            await svc.get_pr_diff(_PR_URL)
        blob = " ".join(str(v) for e in cap for v in e.values())
        assert "tok" not in blob
        assert "os.system" not in blob


# ---------------------------------------------------------------------------
# 125 — compare_changed_files: paths touched between two SHAs (REST compare)
# ---------------------------------------------------------------------------


def _compare_resp(status: int, filenames: list[str]) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.is_success = 200 <= status < 300
    r.json = MagicMock(return_value={"files": [{"filename": f} for f in filenames]})
    return r


class TestCompareChangedFiles:
    @pytest.mark.asyncio
    async def test_success_returns_filenames(self) -> None:
        svc = _make_branch_service()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(
            return_value=_compare_resp(200, ["src/app.py", "docs/readme.md"])
        )
        with patch("httpx.AsyncClient", return_value=mock_client):
            files = await svc.compare_changed_files(_PR_URL, "abc123", "def456")
        assert files == ["src/app.py", "docs/readme.md"]
        assert mock_client.get.await_count == 1

    @pytest.mark.asyncio
    async def test_paginates_until_short_page(self) -> None:
        svc = _make_branch_service()
        page1 = _compare_resp(200, [f"src/f{i}.py" for i in range(100)])
        page2 = _compare_resp(200, ["docs/readme.md"])
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=[page1, page2])
        with patch("httpx.AsyncClient", return_value=mock_client):
            files = await svc.compare_changed_files(_PR_URL, "abc123", "def456")
        assert len(files) == 101
        assert files[-1] == "docs/readme.md"
        assert mock_client.get.await_count == 2

    @pytest.mark.asyncio
    async def test_page_cap_overflow_raises(self) -> None:
        svc = _make_branch_service()
        full = [_compare_resp(200, [f"src/f{i}.py" for i in range(100)])] * 3
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=full)
        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            pytest.raises(RuntimeError),
        ):
            await svc.compare_changed_files(_PR_URL, "abc123", "def456")

    @pytest.mark.asyncio
    async def test_missing_files_list_raises(self) -> None:
        """Copilot r2: a comparison response with no enumerable 'files' list
        (e.g. a >300-file compare omits it) must RAISE, not report zero
        changed files — otherwise the documenting gate would wrongly skip."""
        svc = _make_branch_service()
        r = MagicMock()
        r.status_code = 200
        r.is_success = True
        r.json = MagicMock(return_value={"status": "diverged", "total_commits": 5})
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=r)
        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            pytest.raises(RuntimeError),
        ):
            await svc.compare_changed_files(_PR_URL, "abc123", "def456")

    @pytest.mark.asyncio
    async def test_empty_files_list_is_valid_no_changes(self) -> None:
        """An explicit empty 'files' list (identical commits) is legitimate —
        returns [] without raising."""
        svc = _make_branch_service()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=_compare_resp(200, []))
        with patch("httpx.AsyncClient", return_value=mock_client):
            files = await svc.compare_changed_files(_PR_URL, "abc123", "def456")
        assert files == []

    @pytest.mark.asyncio
    async def test_non_2xx_raises(self) -> None:
        svc = _make_branch_service()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=_compare_resp(404, []))
        with (
            patch("httpx.AsyncClient", return_value=mock_client),
            pytest.raises(RuntimeError),
        ):
            await svc.compare_changed_files(_PR_URL, "gone000", "def456")

    @pytest.mark.asyncio
    async def test_malformed_pr_url_raises(self) -> None:
        svc = _make_branch_service()
        with pytest.raises(ValueError):
            await svc.compare_changed_files("https://example.com/x", "a", "b")

    @pytest.mark.asyncio
    async def test_empty_sha_raises(self) -> None:
        svc = _make_branch_service()
        with pytest.raises(ValueError):
            await svc.compare_changed_files(_PR_URL, "", "def456")

    @pytest.mark.asyncio
    async def test_token_never_logged(self) -> None:
        from structlog.testing import capture_logs

        svc = _make_branch_service()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=_compare_resp(200, ["src/a.py"]))
        with (
            capture_logs() as cap,
            patch("httpx.AsyncClient", return_value=mock_client),
        ):
            await svc.compare_changed_files(_PR_URL, "abc123", "def456")
        blob = " ".join(str(v) for e in cap for v in e.values())
        assert "tok" not in blob
