"""074 — Unit tests for the classify_scope graph node (T030, T031)."""
from __future__ import annotations

from typing import Any

import pytest

from coordinare.graph.nodes.classify_scope import classify_scope_node


class _ScopeCfg:
    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled


class _ConductingCfg:
    def __init__(self, *, model: str = "test/model") -> None:
        self.model = model


class _Cfg:
    def __init__(self, *, enabled: bool, model: str = "test/model") -> None:
        self.persona_scope = _ScopeCfg(enabled=enabled)
        self.conducting = _ConductingCfg(model=model)


def _base_state(*, enabled: bool, pr_url: str | None) -> dict[str, Any]:
    card: dict[str, Any] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    if pr_url:
        card["pr_url"] = pr_url
        card["pr_node_id"] = "PR_kwDO_42"
    session: dict[str, Any] = {"card_id": "ITEM_1", "current_card": card}
    return {
        "config": _Cfg(enabled=enabled),
        "current_card": card,
        "active_card_id": "ITEM_1",
        "active_sessions": {"ITEM_1": session},
        "conducting_backend": None,
        "github_service": None,
    }


# ---------------------------------------------------------------------------
# T030 — disabled is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``persona_scope.enabled`` is False, the node returns state
    unchanged and MUST NOT invoke the classifier.
    """
    called = {"n": 0}

    async def _fake_classify(**kwargs: Any) -> None:
        called["n"] += 1
        return None

    monkeypatch.setattr(
        "coordinare.graph.nodes.classify_scope.persona_classifier.classify",
        _fake_classify,
    )

    state = _base_state(enabled=False, pr_url="https://github.com/acme/repo/pull/42")
    result = await classify_scope_node(state)

    assert called["n"] == 0
    session = result["active_sessions"]["ITEM_1"]
    assert "persona_scope" not in session or session.get("persona_scope") is None


# ---------------------------------------------------------------------------
# T031 — writes persona_scope to session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writes_persona_scope_to_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """When enabled and a PR exists, the node writes the classifier's
    ``PersonaScope`` onto the active session.
    """
    fake_scope: dict[str, Any] = {
        "computed_at": "2026-05-27T00:00:00Z",
        "cycle_index": 1,
        "classifier_model": "test/model",
        "head_sha": "deadbeef",
        "files_summary": [],
        "personas": {
            "reviewer": {"depth": "skim", "focus": "docs", "overrides": []},
            "security": {"depth": "skip", "focus": "no sensitive paths", "overrides": []},
            "qa": {"depth": "skip", "focus": "no runtime code", "overrides": []},
            "tech_writer": {"depth": "full", "focus": "docs change", "overrides": []},
            "closer": {
                "depth": "full",
                "focus": "docs change",
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

    state = _base_state(enabled=True, pr_url="https://github.com/acme/repo/pull/42")
    result = await classify_scope_node(state)

    session = result["active_sessions"]["ITEM_1"]
    assert session["persona_scope"] is fake_scope
    assert session["persona_scope"]["personas"]["reviewer"]["depth"] == "skim"


@pytest.mark.asyncio
async def test_no_pr_does_not_classify(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enabled but no PR yet — classifier MUST NOT be invoked (no diff to
    classify).  First dispatch is always full.
    """
    called = {"n": 0}

    async def _fake_classify(**kwargs: Any) -> None:
        called["n"] += 1
        return None

    monkeypatch.setattr(
        "coordinare.graph.nodes.classify_scope.persona_classifier.classify",
        _fake_classify,
    )

    state = _base_state(enabled=True, pr_url=None)
    result = await classify_scope_node(state)

    assert called["n"] == 0
    session = result["active_sessions"]["ITEM_1"]
    assert session.get("persona_scope") in (None, {}) or "persona_scope" not in session


# ---------------------------------------------------------------------------
# T050 — node runs every cycle when enabled (FR-003)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_runs_every_cycle_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invoking the node twice with the same session must call the classifier
    both times — no caching by head_sha or file list (FR-003).
    """
    calls: list[int] = []

    async def _fake_classify(**kwargs: Any) -> dict[str, Any]:
        idx = len(calls) + 1
        calls.append(idx)
        return {
            "computed_at": f"2026-05-27T00:00:0{idx}Z",
            "cycle_index": idx,
            "classifier_model": "test/model",
            "head_sha": "deadbeef",
            "files_summary": [],
            "personas": {
                "reviewer": {"depth": "skim", "focus": "x", "overrides": []},
                "security": {"depth": "skip", "focus": "x", "overrides": []},
                "qa": {"depth": "skip", "focus": "x", "overrides": []},
                "tech_writer": {"depth": "full", "focus": "x", "overrides": []},
                "closer": {
                    "depth": "full",
                    "focus": "x",
                    "overrides": ["closer_is_scope_invariant"],
                },
            },
        }

    monkeypatch.setattr(
        "coordinare.graph.nodes.classify_scope.persona_classifier.classify",
        _fake_classify,
    )

    state = _base_state(enabled=True, pr_url="https://github.com/acme/repo/pull/42")

    await classify_scope_node(state)
    await classify_scope_node(state)

    assert len(calls) == 2
    # cycle_index advanced on the second invocation.
    assert state["active_sessions"]["ITEM_1"]["persona_scope"]["cycle_index"] == 2
