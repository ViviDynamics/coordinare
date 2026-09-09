"""Spec 074 — End-to-end persona-scope integration test.

Walk a docs-only card through `classify_scope_node` → `dispatch_performer`
across the lifecycle.  The classifier is mocked to emit a docs-only slice:

    reviewer:    skim
    security:    skip
    qa:          skip
    tech_writer: full
    closer:      <ignored — scope-invariant>

Asserts that security/qa never get a dispatch (FR-008) and that the card
advances past them, while reviewer/tech_writer/closing_review do dispatch.
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
from coordinare.graph.nodes.classify_scope import classify_scope_node
from coordinare.graph.nodes.dispatch_performer import dispatch_performer
from coordinare.graph.state import initial_state
from coordinare.workspace import WorkspaceInfo

# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------


class _GitHub:
    async def move_card(self, item_id: str, status: str) -> None:
        pass

    async def find_pr_for_issue(self, issue_id: str) -> dict[str, str] | None:
        return None


class _Service:
    def __init__(self, name: str) -> None:
        self.name = name
        self.dispatched: list[dict[str, Any]] = []

    async def check_health(self) -> dict[str, Any]:
        return {"status": "accepted"}

    async def dispatch_card(
        self, card_context: dict[str, Any], workspace_info: WorkspaceInfo | None = None
    ) -> dict[str, Any]:
        self.dispatched.append(card_context)
        return {"status": "accepted", "session_id": f"sess-{self.name}"}


class _WorkspaceManager:
    async def prepare(self, card: dict[str, Any]) -> WorkspaceInfo:
        return WorkspaceInfo(
            path=Path("/tmp/fake-ws/repo"),
            branch="coordinare/ITEM_1/docs",
            repo_url="https://github.com/acme/repo.git",
            github_token="test-token",
        )

    async def teardown(self, path: Path) -> None:
        pass


class _ScopeCfg:
    def __init__(
        self,
        *,
        path_classes: dict[str, list[str]] | None = None,
        forced_full_on_path_classes: dict[str, list[str]] | None = None,
    ) -> None:
        self.enabled = True
        self.path_classes = path_classes or {"docs": ["*.md", "docs/**/*"]}
        self.classifier_latency_budget_seconds = 30.0
        self.forced_full_on_path_classes = forced_full_on_path_classes or {}


class _ConductingCfg:
    model = "test/model"


def _personas_with_scope_behavior() -> PersonasConfig:
    tier = ScopeTierBehavior(max_tool_calls=5, prompt_addon="scope-narrowed")
    # security & qa carry a placeholder scope_behavior so the FR-008 skip
    # routing applies (per FR-010 — skip requires opt-in via scope_behavior).
    skip_optin = ScopeBehavior(skim=tier)
    return PersonasConfig(
        reviewer=PersonaConfig(instructions="REVIEWER BASE", scope_behavior=ScopeBehavior(skim=tier)),
        security=PersonaConfig(instructions="SECURITY BASE", scope_behavior=skip_optin),
        qa=PersonaConfig(instructions="QA BASE", scope_behavior=skip_optin),
        tech_writer=PersonaConfig(instructions="TW BASE", scope_behavior=ScopeBehavior(full=tier)),
        closer=PersonaConfig(instructions="CLOSER BASE"),
    )


class _Cfg:
    def __init__(self) -> None:
        self.persona_scope = _ScopeCfg()
        self.conducting = _ConductingCfg()
        self.personas = _personas_with_scope_behavior()


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_docs_only_card_skips_security_qa(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_scope: dict[str, Any] = {
        "computed_at": "2026-05-27T00:00:00Z",
        "cycle_index": 1,
        "classifier_model": "test/model",
        "head_sha": "deadbeef",
        "files_summary": [
            {"path": "README.md", "added": 4, "removed": 1, "status": "modified", "classes": ["docs"]},
        ],
        "personas": {
            "reviewer": {"depth": "skim", "focus": "docs only", "overrides": []},
            "security": {"depth": "skip", "focus": "no security surface", "overrides": []},
            "qa": {"depth": "skip", "focus": "no runtime code", "overrides": []},
            "tech_writer": {"depth": "full", "focus": "doc change", "overrides": []},
            "closer": {
                "depth": "full",
                "focus": "doc change",
                "overrides": ["closer_is_scope_invariant"],
            },
        },
    }

    async def _fake_classify(**kwargs: Any) -> Any:
        return fake_scope

    monkeypatch.setattr(
        "coordinare.graph.nodes.classify_scope.persona_classifier.classify",
        _fake_classify,
    )

    card: dict[str, Any] = {
        "id": "ITEM_1",
        "status": "IN_PROGRESS",
        "issue_id": "ISSUE_1",
        "pr_url": "https://github.com/acme/repo/pull/91",
        "pr_node_id": "PR_kwDO_91",
    }
    session: dict[str, Any] = {"card_id": "ITEM_1", "current_card": card}

    state = initial_state()
    state["config"] = _Cfg()
    state["current_card"] = card
    state["active_card_id"] = "ITEM_1"
    state["active_sessions"] = {"ITEM_1": session}
    state["github_service"] = _GitHub()
    state["workspace_manager"] = _WorkspaceManager()

    # Step 1: classify_scope writes the slice to the active session.
    state.update(await classify_scope_node(state))
    assert state["active_sessions"]["ITEM_1"]["persona_scope"] is fake_scope

    # Step 2: walk each stage of the lifecycle through dispatch_performer.
    services = {
        "reviewing": _Service("reviewer"),
        "security": _Service("security"),
        "qa": _Service("qa"),
        "tech_writer": _Service("tech_writer"),
        "closing_review": _Service("closer"),
    }
    state["performer_services"] = services
    state["lifecycle_sequence"] = list(services.keys())

    for stage in list(services.keys()):
        state["performer_stage"] = stage
        # Each dispatch_performer call mutates state in place via return-dict merge.
        result = await dispatch_performer(state)
        state.update(result)

    # FR-008: security and qa MUST NOT have been dispatched (depth=skip).
    assert services["security"].dispatched == []
    assert services["qa"].dispatched == []

    # The non-skipped personas DID dispatch.
    assert len(services["reviewing"].dispatched) == 1
    assert len(services["tech_writer"].dispatched) == 1
    assert len(services["closing_review"].dispatched) == 1

    # Reviewer (skim) carried the configured cap + addon.
    reviewer_ctx = services["reviewing"].dispatched[0]
    assert reviewer_ctx["max_tool_calls"] == 5
    assert reviewer_ctx["scope_addon"] == "scope-narrowed"
    # FR-007: base prompt unmutated.
    assert reviewer_ctx["persona_instructions"] == "REVIEWER BASE" + (
        "\n\nWrite all user-facing progress updates, summaries, findings, and explanations "
        "in English. Preserve code, paths, quoted source text, and machine-readable "
        "protocol fields exactly; do not translate those values."
    )

    # FR-009: closer ignored depth (no cap, no addon) but surfaced focus.
    closer_ctx = services["closing_review"].dispatched[0]
    assert "max_tool_calls" not in closer_ctx
    assert "scope_addon" not in closer_ctx
    assert closer_ctx.get("scope_focus") == "doc change"


@pytest.mark.asyncio
async def test_security_sensitive_card_forces_full(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-005 / US2 end-to-end.

    Classifier emits ``security: skim`` (a small touch on an auth file), but
    operator config marks ``security_sensitive`` as forcing-full for security.
    Walking through dispatch_performer must reflect the forced override:
    the security service IS dispatched with the configured full-tier addon.
    """
    fake_scope_input: dict[str, Any] = {
        # The classifier *would* return skim for security; the forced_full
        # post-processing step overrides to full and tags the override.
        # We mock the classifier directly at the node boundary, but the e2e
        # value of this test is that the *forced* slice flows through
        # dispatch_performer correctly.
        "computed_at": "2026-05-27T00:00:00Z",
        "cycle_index": 1,
        "classifier_model": "test/model",
        "head_sha": "deadbeef",
        "files_summary": [
            {
                "path": "src/auth/session.py",
                "added": 5,
                "removed": 1,
                "status": "modified",
                "classes": ["runtime", "security_sensitive"],
            },
        ],
        "personas": {
            "reviewer": {"depth": "normal", "focus": "small auth touch", "overrides": []},
            "security": {
                "depth": "full",
                "focus": "session.refresh path",
                "overrides": ["forced_full_on_path_class:security_sensitive"],
            },
            "qa": {"depth": "normal", "focus": "retry behavior", "overrides": []},
            "tech_writer": {"depth": "skip", "focus": "no docs", "overrides": []},
            "closer": {
                "depth": "full",
                "focus": "scope-invariant tag",
                "overrides": ["closer_is_scope_invariant"],
            },
        },
    }

    async def _fake_classify(**kwargs: Any) -> Any:
        return fake_scope_input

    monkeypatch.setattr(
        "coordinare.graph.nodes.classify_scope.persona_classifier.classify",
        _fake_classify,
    )

    # Personas config: security gets a full-tier behavior (cap + addon).
    full_tier = ScopeTierBehavior(max_tool_calls=30, prompt_addon="dig deep")
    personas_cfg = PersonasConfig(
        reviewer=PersonaConfig(instructions="REVIEWER BASE"),
        security=PersonaConfig(
            instructions="SECURITY BASE",
            scope_behavior=ScopeBehavior(full=full_tier),
        ),
        closer=PersonaConfig(instructions="CLOSER BASE"),
    )

    class _CfgForcedFull:
        def __init__(self) -> None:
            self.persona_scope = _ScopeCfg(
                path_classes={
                    "runtime": ["src/**/*.py"],
                    "security_sensitive": ["src/auth/*.py"],
                },
                forced_full_on_path_classes={"security": ["security_sensitive"]},
            )
            self.conducting = _ConductingCfg()
            self.personas = personas_cfg

    card: dict[str, Any] = {
        "id": "ITEM_2",
        "status": "IN_PROGRESS",
        "issue_id": "ISSUE_2",
        "pr_url": "https://github.com/acme/repo/pull/92",
        "pr_node_id": "PR_kwDO_92",
    }
    session: dict[str, Any] = {"card_id": "ITEM_2", "current_card": card}

    state = initial_state()
    state["config"] = _CfgForcedFull()
    state["current_card"] = card
    state["active_card_id"] = "ITEM_2"
    state["active_sessions"] = {"ITEM_2": session}
    state["github_service"] = _GitHub()
    state["workspace_manager"] = _WorkspaceManager()

    state.update(await classify_scope_node(state))
    assert state["active_sessions"]["ITEM_2"]["persona_scope"] is fake_scope_input

    services = {
        "reviewing": _Service("reviewer"),
        "security": _Service("security"),
        "closing_review": _Service("closer"),
    }
    state["performer_services"] = services
    state["lifecycle_sequence"] = list(services.keys())

    for stage in list(services.keys()):
        state["performer_stage"] = stage
        result = await dispatch_performer(state)
        state.update(result)

    # Security WAS dispatched (forced from skim → full), with the configured
    # full-tier cap + addon.
    assert len(services["security"].dispatched) == 1
    sec_ctx = services["security"].dispatched[0]
    assert sec_ctx["max_tool_calls"] == 30
    assert sec_ctx["scope_addon"] == "dig deep"
    assert sec_ctx["persona_instructions"] == "SECURITY BASE" + (
        "\n\nWrite all user-facing progress updates, summaries, findings, and explanations "
        "in English. Preserve code, paths, quoted source text, and machine-readable "
        "protocol fields exactly; do not translate those values."
    )


# ---------------------------------------------------------------------------
# US3 end-to-end: classifier failure does not block the card
# ---------------------------------------------------------------------------


class _FailingBackend:
    """Backend whose `prompt(...)` always raises — simulates classifier failure."""

    async def prompt(self, body: str, response_format: str | None = None) -> dict[str, Any]:
        raise RuntimeError("backend exploded")


class _GitHubWithPRFiles(_GitHub):
    async def get_pr_files(
        self, owner: str, repo: str, pr_number: int
    ) -> dict[str, Any]:
        return {
            "files": [
                {
                    "path": "src/app.py",
                    "added": 10,
                    "removed": 2,
                    "status": "modified",
                }
            ],
            "head_sha": "abc123",
        }


@pytest.mark.asyncio
async def test_classifier_failure_does_not_block_card() -> None:
    """FR-006 / US3.

    When the classifier backend fails outright, the card must still flow
    through dispatch with full-everywhere depths.  No mocking of the
    classifier itself — we exercise the real fallback path.
    """
    import time

    from coordinare.services import persona_classifier

    persona_classifier.reset_failure_warning_cooldown()

    full_tier = ScopeTierBehavior(max_tool_calls=30, prompt_addon="dig deep")
    personas_cfg = PersonasConfig(
        reviewer=PersonaConfig(
            instructions="REVIEWER BASE",
            scope_behavior=ScopeBehavior(full=full_tier),
        ),
        security=PersonaConfig(
            instructions="SECURITY BASE",
            scope_behavior=ScopeBehavior(full=full_tier),
        ),
        closer=PersonaConfig(instructions="CLOSER BASE"),
    )

    class _CfgFailing:
        def __init__(self) -> None:
            self.persona_scope = _ScopeCfg(
                path_classes={"runtime": ["src/**/*.py"]},
            )
            self.conducting = _ConductingCfg()
            self.personas = personas_cfg

    card: dict[str, Any] = {
        "id": "ITEM_3",
        "status": "IN_PROGRESS",
        "issue_id": "ISSUE_3",
        "pr_url": "https://github.com/acme/repo/pull/93",
        "pr_node_id": "PR_kwDO_93",
    }
    session: dict[str, Any] = {"card_id": "ITEM_3", "current_card": card}

    state = initial_state()
    state["config"] = _CfgFailing()
    state["current_card"] = card
    state["active_card_id"] = "ITEM_3"
    state["active_sessions"] = {"ITEM_3": session}
    state["github_service"] = _GitHubWithPRFiles()
    state["workspace_manager"] = _WorkspaceManager()
    state["conducting_backend"] = _FailingBackend()

    t0 = time.monotonic()
    state.update(await classify_scope_node(state))
    scope = state["active_sessions"]["ITEM_3"]["persona_scope"]

    # Fallback applied: every classifier persona at depth=full.
    for name in ("reviewer", "security", "qa", "tech_writer"):
        assert scope["personas"][name]["depth"] == "full"
        overrides = scope["personas"][name]["overrides"]
        assert any(o.startswith("classifier_failed:") for o in overrides), overrides
    # Closer is scope-invariant: full + tagged.
    assert scope["personas"]["closer"]["depth"] == "full"
    assert "closer_is_scope_invariant" in scope["personas"]["closer"]["overrides"]

    services = {
        "reviewing": _Service("reviewer"),
        "security": _Service("security"),
        "closing_review": _Service("closer"),
    }
    state["performer_services"] = services
    state["lifecycle_sequence"] = list(services.keys())

    for stage in list(services.keys()):
        state["performer_stage"] = stage
        result = await dispatch_performer(state)
        state.update(result)

    elapsed = time.monotonic() - t0
    assert elapsed < 30.0, f"SC-003 budget exceeded: {elapsed:.2f}s"

    # All non-skipped personas dispatched with the full-tier config.
    assert len(services["reviewing"].dispatched) == 1
    assert len(services["security"].dispatched) == 1
    assert len(services["closing_review"].dispatched) == 1
    assert services["reviewing"].dispatched[0]["max_tool_calls"] == 30
    assert services["security"].dispatched[0]["scope_addon"] == "dig deep"


# ---------------------------------------------------------------------------
# T049 — US4 end-to-end: per-cycle recompute on diff growth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recompute_on_diff_growth(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cycle 1: 12-line diff → all skim. Cycle 2: 240-line diff → all full.
    Assert cycle_index advances and depths reflect the new diff.
    """
    scopes_by_cycle: list[dict[str, Any]] = [
        {
            "computed_at": "2026-05-27T00:00:00Z",
            "cycle_index": 1,
            "classifier_model": "test/model",
            "head_sha": "sha1",
            "files_summary": [
                {"path": "README.md", "added": 12, "removed": 0,
                 "status": "modified", "classes": ["docs"]},
            ],
            "personas": {
                "reviewer": {"depth": "skim", "focus": "tiny", "overrides": []},
                "security": {"depth": "skim", "focus": "tiny", "overrides": []},
                "qa": {"depth": "skim", "focus": "tiny", "overrides": []},
                "tech_writer": {"depth": "skim", "focus": "tiny", "overrides": []},
                "closer": {"depth": "full", "focus": "tiny",
                           "overrides": ["closer_is_scope_invariant"]},
            },
        },
        {
            "computed_at": "2026-05-27T00:05:00Z",
            "cycle_index": 2,
            "classifier_model": "test/model",
            "head_sha": "sha2",
            "files_summary": [
                {"path": "src/app.py", "added": 240, "removed": 0,
                 "status": "modified", "classes": ["runtime"]},
            ],
            "personas": {
                "reviewer": {"depth": "full", "focus": "big", "overrides": []},
                "security": {"depth": "full", "focus": "big", "overrides": []},
                "qa": {"depth": "full", "focus": "big", "overrides": []},
                "tech_writer": {"depth": "full", "focus": "big", "overrides": []},
                "closer": {"depth": "full", "focus": "big",
                           "overrides": ["closer_is_scope_invariant"]},
            },
        },
    ]
    cycle_idx = {"n": 0}

    async def _fake_classify(**kwargs: Any) -> dict[str, Any]:
        scope = scopes_by_cycle[cycle_idx["n"]]
        cycle_idx["n"] += 1
        return scope

    monkeypatch.setattr(
        "coordinare.graph.nodes.classify_scope.persona_classifier.classify",
        _fake_classify,
    )

    skim_tier = ScopeTierBehavior(max_tool_calls=5, prompt_addon="skim-it")
    full_tier = ScopeTierBehavior(max_tool_calls=30, prompt_addon="dig deep")
    personas_cfg = PersonasConfig(
        reviewer=PersonaConfig(
            instructions="REVIEWER BASE",
            scope_behavior=ScopeBehavior(skim=skim_tier, full=full_tier),
        ),
        closer=PersonaConfig(instructions="CLOSER BASE"),
    )

    class _CfgUS4:
        def __init__(self) -> None:
            self.persona_scope = _ScopeCfg()
            self.conducting = _ConductingCfg()
            self.personas = personas_cfg

    card: dict[str, Any] = {
        "id": "ITEM_4",
        "status": "IN_PROGRESS",
        "issue_id": "ISSUE_4",
        "pr_url": "https://github.com/acme/repo/pull/94",
        "pr_node_id": "PR_kwDO_94",
    }
    session: dict[str, Any] = {"card_id": "ITEM_4", "current_card": card}

    state = initial_state()
    state["config"] = _CfgUS4()
    state["current_card"] = card
    state["active_card_id"] = "ITEM_4"
    state["active_sessions"] = {"ITEM_4": session}
    state["github_service"] = _GitHub()
    state["workspace_manager"] = _WorkspaceManager()

    # Cycle 1
    state.update(await classify_scope_node(state))
    assert state["active_sessions"]["ITEM_4"]["persona_scope"]["cycle_index"] == 1
    services_c1 = {"reviewing": _Service("reviewer")}
    state["performer_services"] = services_c1
    state["lifecycle_sequence"] = ["reviewing"]
    state["performer_stage"] = "reviewing"
    state.update(await dispatch_performer(state))
    assert services_c1["reviewing"].dispatched[0]["max_tool_calls"] == 5

    # Cycle 2 — diff has grown.
    state.update(await classify_scope_node(state))
    scope = state["active_sessions"]["ITEM_4"]["persona_scope"]
    assert scope["cycle_index"] == 2
    assert scope["head_sha"] == "sha2"
    assert scope["personas"]["reviewer"]["depth"] == "full"
    services_c2 = {"reviewing": _Service("reviewer")}
    state["performer_services"] = services_c2
    state["lifecycle_sequence"] = ["reviewing"]
    state["performer_stage"] = "reviewing"
    state.update(await dispatch_performer(state))
    assert services_c2["reviewing"].dispatched[0]["max_tool_calls"] == 30

    assert cycle_idx["n"] == 2


# ---------------------------------------------------------------------------
# T057 — US5 end-to-end: shadow mode (classifier runs, no behavior changes)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shadow_mode_classifier_runs_but_no_persona_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-010 / US5: feature enabled, path_classes set, but no persona has
    ``scope_behavior`` configured. The classifier still runs (operator can
    inspect its output), but dispatches must match today's defaults — no
    ``max_tool_calls`` or ``scope_addon`` injected, no personas skipped.
    """
    classifier_calls = {"n": 0}
    fake_scope: dict[str, Any] = {
        "computed_at": "2026-05-27T00:00:00Z",
        "cycle_index": 1,
        "classifier_model": "test/model",
        "head_sha": "deadbeef",
        "files_summary": [
            {"path": "README.md", "added": 4, "removed": 1,
             "status": "modified", "classes": ["docs"]},
        ],
        "personas": {
            "reviewer": {"depth": "skim", "focus": "docs", "overrides": []},
            "security": {"depth": "skip", "focus": "no security", "overrides": []},
            "qa": {"depth": "skip", "focus": "no runtime", "overrides": []},
            "tech_writer": {"depth": "full", "focus": "docs", "overrides": []},
            "closer": {"depth": "full", "focus": "docs",
                       "overrides": ["closer_is_scope_invariant"]},
        },
    }

    async def _fake_classify(**kwargs: Any) -> Any:
        classifier_calls["n"] += 1
        return fake_scope

    monkeypatch.setattr(
        "coordinare.graph.nodes.classify_scope.persona_classifier.classify",
        _fake_classify,
    )

    # No persona has scope_behavior — additive default (FR-010).
    personas_cfg = PersonasConfig(
        reviewer=PersonaConfig(instructions="REVIEWER BASE"),
        security=PersonaConfig(instructions="SECURITY BASE"),
        qa=PersonaConfig(instructions="QA BASE"),
        tech_writer=PersonaConfig(instructions="TW BASE"),
        closer=PersonaConfig(instructions="CLOSER BASE"),
    )

    class _CfgShadow:
        def __init__(self) -> None:
            self.persona_scope = _ScopeCfg()
            self.conducting = _ConductingCfg()
            self.personas = personas_cfg

    card: dict[str, Any] = {
        "id": "ITEM_5",
        "status": "IN_PROGRESS",
        "issue_id": "ISSUE_5",
        "pr_url": "https://github.com/acme/repo/pull/95",
        "pr_node_id": "PR_kwDO_95",
    }
    session: dict[str, Any] = {"card_id": "ITEM_5", "current_card": card}

    state = initial_state()
    state["config"] = _CfgShadow()
    state["current_card"] = card
    state["active_card_id"] = "ITEM_5"
    state["active_sessions"] = {"ITEM_5": session}
    state["github_service"] = _GitHub()
    state["workspace_manager"] = _WorkspaceManager()

    state.update(await classify_scope_node(state))
    # Classifier did run — its output is available for inspection.
    assert classifier_calls["n"] == 1
    assert state["active_sessions"]["ITEM_5"]["persona_scope"] is fake_scope

    services = {
        "reviewing": _Service("reviewer"),
        "security": _Service("security"),
        "qa": _Service("qa"),
        "tech_writer": _Service("tech_writer"),
        "closing_review": _Service("closer"),
    }
    state["performer_services"] = services
    state["lifecycle_sequence"] = list(services.keys())

    for stage in list(services.keys()):
        state["performer_stage"] = stage
        result = await dispatch_performer(state)
        state.update(result)

    # FR-010: shadow mode — all personas dispatched, none skipped, no
    # scope-tier injection.
    for name in ("reviewing", "security", "qa", "tech_writer", "closing_review"):
        assert len(services[name].dispatched) == 1, f"{name} not dispatched"
        ctx = services[name].dispatched[0]
        assert "max_tool_calls" not in ctx, f"{name} got scope cap in shadow mode"
        assert "scope_addon" not in ctx, f"{name} got scope addon in shadow mode"
