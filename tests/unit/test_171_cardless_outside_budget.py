"""171: the card-less daemon dispatches run outside max_concurrent_cards, by decision.

Env bootstrap, the advocate and curator intake (173), and wiki-init (124) own no
card, so they hold no pipeline slot. Each is bounded by its own gate: a per-symphony
in-flight marker, a scan interval, or an operator request. These tests pin that
behaviour with the budget already full, so a later change that starts counting them
against a card slot fails here and has to update the documented decision too.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.config import (
    AdvocateConfig,
    CuratorConfig,
    PerformerRoleConfig,
    PerformersConfig,
    ProjectConfiguration,
)
from coordinare.daemon import CoordinareDaemon
from coordinare.models.env_cache import BootstrapJobPayload, EnvCacheState
from coordinare.services.pipeline_budget import select_pipelines
from tests.unit.env_mount_stub import mount_global


def _busy_card_session() -> dict[str, Any]:
    """One card holds the only slot: a live lifecycle performer."""
    return {
        "card_id": "A",
        "phase": "monitoring_performer",
        "current_card": {"id": "A", "status": "IN_PROGRESS"},
        "agent_dispatch": {"session_id": "perf-A"},
        "documenting_side": None,
    }


def _full_budget_daemon() -> CoordinareDaemon:
    daemon = CoordinareDaemon.__new__(CoordinareDaemon)
    daemon._state = {
        "active_sessions": {"A": _busy_card_session()},
        "config": SimpleNamespace(max_concurrent_cards=1),
    }
    daemon._intake_poll_tasks = set()
    daemon._wiki_init_poll_tasks = set()
    daemon._bootstrap_poll_tasks = set()
    return daemon


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")


def test_the_budget_is_full_for_the_fixture() -> None:
    sessions = {"A": _busy_card_session()}
    assert select_pipelines(sessions, 1) == {"A"}
    waiting = {"B": {"card_id": "B", "phase": "assessing", "current_card": {"id": "B", "status": "IN_PROGRESS"}}}
    assert select_pipelines(sessions | waiting, 1) == {"A"}


@pytest.mark.parametrize("role", ["advocate", "curator"])
@pytest.mark.asyncio
async def test_intake_runs_dispatch_with_the_budget_full(role: str) -> None:
    svc = MagicMock()
    svc.dispatch_card = AsyncMock(return_value={"session_id": "intake-1"})
    cfg = SimpleNamespace(
        advocate=AdvocateConfig(enabled=True, github_repo="r"),
        curator=CuratorConfig(enabled=True, github_repo="r"),
        github_org="o",
        personas=SimpleNamespace(),
        performers=SimpleNamespace(resolved_role=lambda _n: None),
        resolve_performer_dispatch_model=lambda _r: {},
        max_concurrent_cards=1,
    )
    daemon = _full_budget_daemon()
    daemon._state |= {
        "config": cfg,
        "env_cache": {"s": EnvCacheState(symphony_name="s", sanitised_name="s", cache_dir=Path("/tmp/s"))},
        "performer_services": {"assessing": svc},
        "symphony_workspace_managers": {},
    }
    await daemon._maybe_dispatch_intake(role, "s", SimpleNamespace(project_id=""))
    svc.dispatch_card.assert_awaited_once()
    assert svc.dispatch_card.call_args.args[0]["role"] == role
    assert daemon._state["active_sessions"]["A"]["phase"] == "monitoring_performer"


@pytest.mark.asyncio
async def test_wiki_init_dispatches_with_the_budget_full() -> None:
    daemon = _full_budget_daemon()
    ec = EnvCacheState(symphony_name="sym", sanitised_name="sym", cache_dir=Path("/tmp/x"))
    wm = MagicMock()
    wm.get_fresh_github_token = AsyncMock(return_value="tok")
    gh = MagicMock()
    gh.org = "acme"
    gh._project_name = "repo"
    svc = MagicMock()
    svc.dispatch_card = AsyncMock(return_value={"session_id": "wiki-1"})
    daemon._state |= {
        "env_cache": {"sym": ec},
        "symphony_workspace_managers": {"sym": wm},
        "performer_services": {"documenting": svc},
        "config": None,
    }
    daemon._poll_wiki_init_completion = AsyncMock(return_value=None)
    await daemon._execute_wiki_init_dispatch("sym", gh)
    svc.dispatch_card.assert_awaited_once()
    assert ec.wiki_in_flight is True
    assert daemon._state["active_sessions"]["A"]["phase"] == "monitoring_performer"


@pytest.mark.asyncio
async def test_env_bootstrap_dispatches_with_the_budget_full() -> None:
    daemon = _full_budget_daemon()
    role = PerformerRoleConfig(backend="codex", workflow="env_bootstrap")
    daemon._state |= {
        "config": SimpleNamespace(
            max_concurrent_cards=1,
            performers=PerformersConfig(env_bootstrap=role,
                                        implementer=PerformerRoleConfig(backend="codex", workflow="implementer")),
        ),
        "coordinare_config": SimpleNamespace(bootstrap_max_seconds=60, global_config=mount_global()),
    }
    service = MagicMock()
    service.dispatch_card = AsyncMock(return_value={"status": "error", "reason": "fixture"})
    payload = BootstrapJobPayload(symphony_name="sym", symphony_org="o", symphony_repo="r", cache_mount_path="/devenv/sym")
    await daemon._execute_bootstrap_dispatch("bootstrap", payload, "sym", {"bootstrap": service}, MagicMock())
    service.dispatch_card.assert_awaited_once()
    assert daemon._state["active_sessions"]["A"]["phase"] == "monitoring_performer"


def test_the_config_field_documents_which_dispatches_sit_outside_the_budget() -> None:
    description = (ProjectConfiguration.model_fields["max_concurrent_cards"].description or "").lower()
    assert "outside this budget" in description
    for name in ("env bootstrap", "advocate", "curator", "wiki-init"):
        assert name in description
    assert "side run" in description
