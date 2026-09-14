"""089 T003a(a) — Config-delivery test for the implementer local test gate.

The gate is coordinare-configured but executes inside the performer, which only
sees fields delivered on the dispatch payload. ``dispatch_performer`` must
resolve the symphony's ``persona_scope.local_test_gate`` (mirror
``_get_ci_gate_config``) and inject ``card_context["local_test_gate"]`` for the
``implementer`` role — and ONLY that role. Without this leg of the C1 chain the
gate silently no-ops in prod regardless of config.

Mirrors the mocks/patterns in ``test_dispatch_performer_scope.py``.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.config import LocalTestGateConfig, PersonaScopeConfig
from coordinare.graph.nodes.dispatch_performer import dispatch_performer
from coordinare.graph.state import initial_state
from coordinare.workspace import WorkspaceInfo


class _GitHub:
    async def move_card(self, item_id: str, status: str) -> None:
        pass

    async def find_pr_for_issue(self, issue_id: str) -> dict[str, str] | None:
        return None


class _Service:
    def __init__(self) -> None:
        self.dispatched: list[dict[str, Any]] = []

    async def check_health(self) -> dict[str, Any]:
        return {"status": "accepted"}

    async def dispatch_card(
        self, card_context: dict[str, Any], workspace_info: WorkspaceInfo | None = None
    ) -> dict[str, Any]:
        self.dispatched.append(card_context)
        return {"status": "accepted", "session_id": "sess-1"}


class _WorkspaceManager:
    async def prepare(self, card: dict[str, Any]) -> WorkspaceInfo:
        return WorkspaceInfo(
            path=Path("/tmp/fake-ws/repo"),
            branch="coordinare/ITEM_1/test-card",
            repo_url="https://github.com/acme/repo.git",
            github_token="test-token",
        )

    async def teardown(self, path: Path) -> None:
        pass


def _state(
    *,
    performer_stage: str,
    local_test_gate: LocalTestGateConfig | None,
) -> dict[str, Any]:
    card: dict[str, Any] = {
        "id": "ITEM_1",
        "status": "IN_PROGRESS",
        "issue_id": "ISSUE_1",
    }
    session = {"card_id": "ITEM_1", "current_card": card, "persona_scope": None}
    state = initial_state()
    state["current_card"] = card
    state["active_card_id"] = "ITEM_1"
    state["active_sessions"] = {"ITEM_1": session}
    state["github_service"] = _GitHub()
    state["workspace_manager"] = _WorkspaceManager()
    state["performer_stage"] = performer_stage
    state["lifecycle_sequence"] = [performer_stage]
    state["config"] = SimpleNamespace()
    state["current_symphony"] = "default"
    if local_test_gate is not None:
        sym_cfg = SimpleNamespace(
            persona_scope=PersonaScopeConfig(local_test_gate=local_test_gate)
        )
        state["symphony_configs"] = {"default": sym_cfg}
    return state


@pytest.mark.asyncio
async def test_gate_injected_for_implementer_when_enabled() -> None:
    """Enabled gate → card_context carries enabled + timeout_seconds for the implementer."""
    svc = _Service()
    state = _state(
        performer_stage="implementing",
        local_test_gate=LocalTestGateConfig(enabled=True, timeout_seconds=600),
    )
    state["performer_services"] = {"implementing": svc}

    result = await dispatch_performer(state)

    assert result["phase"] == "monitoring_performer"
    ctx = svc.dispatched[0]
    # 409: the payload also carries the operator overrides; defaults are
    # detection-only (None commands, empty pattern/root lists).
    assert ctx["local_test_gate"] == {
        "enabled": True,
        "timeout_seconds": 600,
        "command": None,
        "lint_command": None,
        "test_path_patterns": [],
        "roots": [],
    }


@pytest.mark.asyncio
async def test_gate_not_injected_when_unconfigured() -> None:
    """No symphony config → no gate key (or disabled); behaviour byte-identical (SC-005)."""
    svc = _Service()
    state = _state(performer_stage="implementing", local_test_gate=None)
    state["performer_services"] = {"implementing": svc}

    result = await dispatch_performer(state)

    assert result["phase"] == "monitoring_performer"
    ctx = svc.dispatched[0]
    assert ctx.get("local_test_gate", {}).get("enabled", False) is False


@pytest.mark.asyncio
async def test_gate_not_injected_for_non_implementer_role() -> None:
    """The gate is implementer-only — a reviewer dispatch never carries it."""
    svc = _Service()
    state = _state(
        performer_stage="reviewing",
        local_test_gate=LocalTestGateConfig(enabled=True, timeout_seconds=600),
    )
    # reviewing is PR-gated; supply a PR so dispatch proceeds.
    state["current_card"]["pr_url"] = "https://github.com/acme/repo/pull/42"
    state["current_card"]["pr_node_id"] = "PR_kwDO_42"
    state["active_sessions"]["ITEM_1"]["current_card"] = state["current_card"]
    state["performer_services"] = {"reviewing": svc}

    result = await dispatch_performer(state)

    assert result["phase"] == "monitoring_performer"
    ctx = svc.dispatched[0]
    assert "local_test_gate" not in ctx
