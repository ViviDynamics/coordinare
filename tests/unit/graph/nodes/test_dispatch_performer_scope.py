"""074 — Unit tests for persona-scope wiring inside dispatch_performer.

Covers T032, T033, T034 (US1) and T054, T055 (US5) from
``specs/074-persona-scope-tiering/tasks.md``.  These tests exercise the
behavior added in `dispatch_performer.py` for reading the
``session.persona_scope`` slice for the dispatched persona and applying its
``max_tool_calls`` / ``prompt_addon`` from the persona's ``scope_behavior``
config — without mutating the persona's base prompt (FR-007).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from coordinare.config import (
    PersonaConfig,
    PersonasConfig,
    ScopeBehavior,
    ScopeTierBehavior,
)
from coordinare.graph.nodes.dispatch_performer import dispatch_performer
from coordinare.graph.state import initial_state
from coordinare.workspace import WorkspaceInfo

# ---------------------------------------------------------------------------
# Mocks (mirrors test_dispatch_performer.py patterns)
# ---------------------------------------------------------------------------


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
        self, card_context: dict[str, Any], workspace_info: WorkspaceInfo | None = None,
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config_with_personas(personas: PersonasConfig) -> Any:
    class _FakeConfig:
        pass

    cfg = _FakeConfig()
    cfg.personas = personas
    return cfg


def _state_with_scope(
    *,
    performer_stage: str,
    role: str,
    persona_scope_slice: dict[str, Any] | None,
    personas: PersonasConfig,
    pr_url: str = "https://github.com/acme/repo/pull/42",
    pr_node_id: str = "PR_kwDO_42",
) -> dict[str, Any]:
    card: dict[str, Any] = {
        "id": "ITEM_1",
        "status": "IN_PROGRESS",
        "issue_id": "ISSUE_1",
        "pr_url": pr_url,
        "pr_node_id": pr_node_id,
    }
    persona_scope = None
    if persona_scope_slice is not None:
        persona_scope = {
            "computed_at": "2026-05-27T00:00:00Z",
            "cycle_index": 1,
            "classifier_model": "test/model",
            "head_sha": "deadbeef",
            "files_summary": [],
            "personas": {role: persona_scope_slice},
        }
    session = {
        "card_id": "ITEM_1",
        "current_card": card,
        "persona_scope": persona_scope,
    }
    state = initial_state()
    state["current_card"] = card
    state["active_card_id"] = "ITEM_1"
    state["active_sessions"] = {"ITEM_1": session}
    state["github_service"] = _GitHub()
    state["workspace_manager"] = _WorkspaceManager()
    state["performer_stage"] = performer_stage
    state["lifecycle_sequence"] = [performer_stage]
    state["config"] = _config_with_personas(personas)
    return state


# ---------------------------------------------------------------------------
# T032 — max_tool_calls applied from scope_behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_tool_calls_applied() -> None:
    """When reviewer.scope_behavior.skim.max_tool_calls=5 and the slice has
    depth='skim', the dispatched payload carries ``max_tool_calls=5``.
    """
    reviewer = PersonaConfig(
        instructions="REVIEWER BASE",
        scope_behavior=ScopeBehavior(
            skim=ScopeTierBehavior(max_tool_calls=5, prompt_addon="skim addon"),
        ),
    )
    personas = PersonasConfig(reviewer=reviewer)
    svc = _Service()

    state = _state_with_scope(
        performer_stage="reviewing",
        role="reviewer",
        persona_scope_slice={"depth": "skim", "focus": "docs only", "overrides": []},
        personas=personas,
    )
    state["performer_services"] = {"reviewing": svc}

    result = await dispatch_performer(state)

    assert result["phase"] == "monitoring_performer"
    assert len(svc.dispatched) == 1
    ctx = svc.dispatched[0]
    assert ctx["max_tool_calls"] == 5


# ---------------------------------------------------------------------------
# T033 — prompt_addon is a structured input field, not a base-prompt mutation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_addon_is_structured_input_not_base_mutation() -> None:
    """FR-007: persona_instructions must be the unmodified base; the addon is
    surfaced as a separate ``scope_addon`` field in card_context.
    """
    base = "REVIEWER BASE PROMPT BODY"
    addon = "Scope-narrowed: review only ARCHITECTURE.md."
    reviewer = PersonaConfig(
        instructions=base,
        scope_behavior=ScopeBehavior(
            skim=ScopeTierBehavior(max_tool_calls=4, prompt_addon=addon),
        ),
    )
    personas = PersonasConfig(reviewer=reviewer)
    svc = _Service()

    state = _state_with_scope(
        performer_stage="reviewing",
        role="reviewer",
        persona_scope_slice={"depth": "skim", "focus": "docs", "overrides": []},
        personas=personas,
    )
    state["performer_services"] = {"reviewing": svc}

    result = await dispatch_performer(state)

    assert result["phase"] == "monitoring_performer"
    ctx = svc.dispatched[0]
    # Base prompt is unmutated …
    assert ctx["persona_instructions"] == base + ("\n\nWrite all user-facing progress updates, summaries, findings, and explanations "
            "in English. Preserve code, paths, quoted source text, and machine-readable "
            "protocol fields exactly; do not translate those values.")
    assert addon not in ctx["persona_instructions"]
    # … and the addon arrives in its own structured field.
    assert ctx["scope_addon"] == addon


# ---------------------------------------------------------------------------
# T034 — closer is scope-invariant (ignores depth, consumes focus)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_closer_ignores_depth_consumes_focus() -> None:
    """FR-009: closer ignores the slice's ``depth`` (never throttled), but the
    slice's ``focus`` is passed in as advisory context.
    """
    closer = PersonaConfig(
        instructions="CLOSER BASE",
        # Even if a (misconfigured) scope_behavior is present, closer must
        # ignore depth and never apply max_tool_calls / prompt_addon.
        scope_behavior=ScopeBehavior(
            skim=ScopeTierBehavior(max_tool_calls=2, prompt_addon="should be ignored"),
        ),
    )
    personas = PersonasConfig(closer=closer)
    svc = _Service()

    state = _state_with_scope(
        performer_stage="closing_review",
        role="closer",
        persona_scope_slice={
            "depth": "skim",  # would normally throttle non-closer personas
            "focus": "Auth middleware change",
            "overrides": ["closer_is_scope_invariant"],
        },
        personas=personas,
    )
    state["performer_services"] = {"closing_review": svc}

    result = await dispatch_performer(state)

    assert result["phase"] == "monitoring_performer"
    ctx = svc.dispatched[0]
    # Closer was dispatched at full behavior — no tool-call cap, no addon.
    assert "max_tool_calls" not in ctx
    assert "scope_addon" not in ctx
    # But the slice's focus IS surfaced as advisory context.
    assert ctx.get("scope_focus") == "Auth middleware change"


# ---------------------------------------------------------------------------
# T054 — persona without scope_behavior runs as today (FR-010 additive)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persona_without_scope_behavior_runs_as_today() -> None:
    """If a persona has no ``scope_behavior`` block, the PersonaScope slice is
    effectively ignored at dispatch time: no max_tool_calls cap, no addon.
    """
    reviewer = PersonaConfig(instructions="REVIEWER BASE", scope_behavior=None)
    personas = PersonasConfig(reviewer=reviewer)
    svc = _Service()

    state = _state_with_scope(
        performer_stage="reviewing",
        role="reviewer",
        persona_scope_slice={"depth": "skim", "focus": "docs", "overrides": []},
        personas=personas,
    )
    state["performer_services"] = {"reviewing": svc}

    result = await dispatch_performer(state)

    assert result["phase"] == "monitoring_performer"
    ctx = svc.dispatched[0]
    assert "max_tool_calls" not in ctx
    assert "scope_addon" not in ctx
    # The base prompt is still set, and focus may still be surfaced.
    assert ctx["persona_instructions"] == "REVIEWER BASE" + ("\n\nWrite all user-facing progress updates, summaries, findings, and explanations "
            "in English. Preserve code, paths, quoted source text, and machine-readable "
            "protocol fields exactly; do not translate those values.")


# ---------------------------------------------------------------------------
# T055 — missing tier falls back to today's defaults
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_tier_falls_back_to_today() -> None:
    """When scope_behavior only defines `full` but the slice asks for `skim`,
    no override is applied (no cap, no addon).  Documented as a debug log in
    the contract — we just verify the behavioral fallback here.
    """
    reviewer = PersonaConfig(
        instructions="REVIEWER BASE",
        scope_behavior=ScopeBehavior(
            full=ScopeTierBehavior(max_tool_calls=20, prompt_addon="full addon"),
            # skim / normal intentionally omitted
        ),
    )
    personas = PersonasConfig(reviewer=reviewer)
    svc = _Service()

    state = _state_with_scope(
        performer_stage="reviewing",
        role="reviewer",
        persona_scope_slice={"depth": "skim", "focus": "docs", "overrides": []},
        personas=personas,
    )
    state["performer_services"] = {"reviewing": svc}

    result = await dispatch_performer(state)

    assert result["phase"] == "monitoring_performer"
    ctx = svc.dispatched[0]
    assert "max_tool_calls" not in ctx
    assert "scope_addon" not in ctx
