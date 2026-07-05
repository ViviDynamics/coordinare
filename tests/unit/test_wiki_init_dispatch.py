"""124 (US2): daemon wiki-init dispatch/poll — the 'Init wiki' button's backend.

Unit-level coverage of the manual-trigger orchestration (the cardless documenter
init dispatch + poll -> WikiInitService.handle_init_result). Full end-to-end needs
a live symphony + performer; these lock the wiring/contract.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import coordinare.daemon as dmod
from coordinare.daemon import CoordinareDaemon
from coordinare.models.env_cache import EnvCacheState


class _Graph:
    async def ainvoke(self, state):
        return dict(state)


async def _no_sleep(_: int) -> None:
    return None


def _daemon() -> CoordinareDaemon:
    return CoordinareDaemon(
        _Graph(), poll_interval_seconds=1, heartbeat_interval_seconds=1,
        max_cycles=1, sleep_func=_no_sleep,
    )


def _ec() -> EnvCacheState:
    return EnvCacheState(symphony_name="sym", sanitised_name="sym", cache_dir=Path("/tmp/x"))


def _github() -> MagicMock:
    gh = MagicMock()
    gh.org = "acme"
    gh._project_name = "repo"
    gh.get_token = AsyncMock(return_value="tok")
    return gh


async def _noop_poll(*_a, **_k) -> None:
    return None


def _workspace_managers(token: str = "tok") -> dict[str, MagicMock]:
    """A cardless wiki-init dispatch sources its GITHUB_TOKEN from the symphony's
    workspace manager (GitHubService has no get_token()); mirror that here."""
    wm = MagicMock()
    wm.get_fresh_github_token = AsyncMock(return_value=token)
    return {"sym": wm}


@pytest.mark.asyncio
async def test_execute_wiki_init_dispatch_builds_cardless_init_context() -> None:
    d = _daemon()
    ec = _ec()
    d._state["env_cache"] = {"sym": ec}
    d._state["symphony_workspace_managers"] = _workspace_managers("tok")
    svc = MagicMock()
    svc.dispatch_card = AsyncMock(return_value={"session_id": "s1"})
    d._state["performer_services"] = {"documenting": svc}
    d._state["config"] = None
    d._poll_wiki_init_completion = _noop_poll  # don't spawn a real 30-min poll
    await d._execute_wiki_init_dispatch("sym", _github())
    svc.dispatch_card.assert_awaited_once()
    cc = svc.dispatch_card.call_args[0][0]
    assert cc["role"] == "documenting"
    assert cc["doc_mode"] == "init"
    assert cc["repo_url"] == "https://github.com/acme/repo.git"
    assert cc["branch"].startswith("wiki-init/sym")  # sanitised name may carry a hash suffix
    assert cc["title"]
    # The GITHUB_TOKEN secret is derived from workspace_info.github_token for the
    # documenting role (a cardless dispatch never runs WorkspaceManager.prepare()).
    # Without this the performer fails "permanent performer config error: GITHUB_TOKEN".
    wi = svc.dispatch_card.call_args.kwargs["workspace_info"]
    assert wi.github_token == "tok"
    assert wi.path is None  # performer self-clones
    assert wi.repo_url == "https://github.com/acme/repo.git"
    assert wi.branch == cc["branch"]
    assert ec.wiki_in_flight is True


@pytest.mark.asyncio
async def test_execute_wiki_init_dispatch_fails_fast_without_github_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No workspace-manager token and no GITHUB_TOKEN env => do NOT dispatch a
    doomed job the performer rejects with 'permanent performer config error:
    GITHUB_TOKEN'. wiki_in_flight must not be left set."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    d = _daemon()
    ec = _ec()
    d._state["env_cache"] = {"sym": ec}
    d._state["symphony_workspace_managers"] = {}  # no token source
    svc = MagicMock()
    svc.dispatch_card = AsyncMock()
    d._state["performer_services"] = {"documenting": svc}
    d._state["config"] = None
    await d._execute_wiki_init_dispatch("sym", _github())
    svc.dispatch_card.assert_not_awaited()
    assert ec.wiki_in_flight is False


@pytest.mark.asyncio
async def test_execute_wiki_init_dispatch_noop_without_documenting_service() -> None:
    d = _daemon()
    d._state["env_cache"] = {"sym": _ec()}
    d._state["performer_services"] = {}  # no documenting service
    d._state["config"] = None
    await d._execute_wiki_init_dispatch("sym", _github())  # must not raise


@pytest.mark.asyncio
async def test_execute_wiki_init_dispatch_skips_when_in_flight() -> None:
    d = _daemon()
    ec = _ec()
    ec.wiki_in_flight = True
    d._state["env_cache"] = {"sym": ec}
    svc = MagicMock()
    svc.dispatch_card = AsyncMock()
    d._state["performer_services"] = {"documenting": svc}
    d._state["config"] = None
    await d._execute_wiki_init_dispatch("sym", _github())
    svc.dispatch_card.assert_not_awaited()  # already in flight


@pytest.mark.asyncio
async def test_poll_wiki_init_completion_hands_pr_to_handle_result() -> None:
    d = _daemon()
    ec = _ec()
    ec.wiki_in_flight = True
    d._state["env_cache"] = {"sym": ec}
    d._state["config"] = None
    d._state["notification_service"] = None
    svc = MagicMock()
    svc.check_status = AsyncMock(return_value={"status": "docs_committed", "pr_node_id": "PR1"})
    d._wiki_init_svc.handle_init_result = AsyncMock()
    with patch.object(dmod.asyncio, "sleep", new=AsyncMock()):
        await d._poll_wiki_init_completion("sym", svc, "s1", _github())
    d._wiki_init_svc.handle_init_result.assert_awaited_once()
    args, kwargs = d._wiki_init_svc.handle_init_result.call_args
    assert args[3] == "PR1"  # pr_node_id passed through
    assert kwargs["job_succeeded"] is True


@pytest.mark.asyncio
async def test_poll_wiki_init_completion_reports_failure() -> None:
    d = _daemon()
    ec = _ec()
    ec.wiki_in_flight = True
    d._state["env_cache"] = {"sym": ec}
    d._state["config"] = None
    d._state["notification_service"] = None
    svc = MagicMock()
    svc.check_status = AsyncMock(return_value={"status": "error", "reason": "boom"})
    d._wiki_init_svc.handle_init_result = AsyncMock()
    with patch.object(dmod.asyncio, "sleep", new=AsyncMock()):
        await d._poll_wiki_init_completion("sym", svc, "s1", _github())
    _, kwargs = d._wiki_init_svc.handle_init_result.call_args
    assert kwargs["job_succeeded"] is False
