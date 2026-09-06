"""165 FR-010 / FR-013 / FR-014: each reader gets exactly its slice; nothing
else; nothing when there is no blueprint."""
from __future__ import annotations

import pytest

from coordinare.graph.nodes.dispatch_performer import (
    documenter_side_run_wanted,
    inject_briefs,
    project_brief,
)

_BP = {
    "summary": "s",
    "milestones": [{"goal": "g", "scope": ["a"], "done_when": "d"}, {"goal": "g2", "scope": [], "done_when": "d2"}],
    "modules": [{"path": "app/", "note": "n"}],
    "data_model": {"changes": [{"kind": "column", "name": "c", "note": "n"}]},
    "interfaces": [{"name": "i", "kind": "endpoint", "contract": "c"}],
    "risks": ["r"],
    "criteria": [{"surface": "/", "action": "open", "expected": "ok", "kind": "functional"}],
    "docs": [{"topic": "t", "location": "l", "say": "s"}],
    "size": "large",
    "blueprint_hash": "h",
    "created_at": "t",
}


def _ctx(role: str, blueprint=_BP) -> dict:
    ctx: dict = {}
    inject_briefs(ctx, {"blueprint": blueprint}, role=role)
    return ctx


def test_implementer_gets_the_implementation_brief_and_nothing_else():
    ctx = _ctx("implementing")
    brief = ctx["implementation_brief"]
    assert set(brief) == {"summary", "milestones", "modules", "data_model", "interfaces", "risks", "size"}
    assert "docs" not in brief and "criteria" not in brief
    assert "documentation_brief" not in ctx and "verification_brief" not in ctx
    assert "implementer_single_turn" not in ctx, "large blueprints keep the milestone loop"


def test_documenter_gets_only_docs_summary_and_modules():
    ctx = _ctx("documenting")
    assert set(ctx["documentation_brief"]) == {"summary", "docs", "modules"}
    assert "implementation_brief" not in ctx and "verification_brief" not in ctx


def test_qa_gets_only_criteria_and_summary():
    ctx = _ctx("qa")
    assert set(ctx["verification_brief"]) == {"summary", "criteria"}
    assert "milestones" not in ctx["verification_brief"] and "docs" not in ctx["verification_brief"]


@pytest.mark.parametrize("role", ["assessing", "architecting", "reviewing", "security", "closing_review", None])
def test_other_roles_get_no_brief(role):
    assert _ctx(role) == {}


def test_no_blueprint_means_no_keys_at_all():
    for role in ("implementing", "documenting", "qa"):
        ctx: dict = {}
        inject_briefs(ctx, {}, role=role)
        inject_briefs(ctx, {"blueprint": None}, role=role)
        inject_briefs(ctx, {"blueprint": {"milestones": []}}, role=role)
        assert ctx == {}


def test_small_blueprint_sets_single_turn_for_the_implementer_only():
    small = {**_BP, "size": "small"}
    assert _ctx("implementing", small)["implementer_single_turn"] is True
    assert "implementer_single_turn" not in _ctx("qa", small)
    assert "implementer_single_turn" not in _ctx("documenting", small)


def test_briefs_are_copies_not_views():
    ctx = _ctx("implementing")
    ctx["implementation_brief"]["milestones"][0]["goal"] = "mutated"
    assert _BP["milestones"][0]["goal"] == "g"


def test_project_brief_skips_missing_fields():
    assert project_brief({"summary": "s"}, ("summary", "docs")) == {"summary": "s"}


def test_documenter_side_run_wanted_only_for_non_empty_docs():
    assert documenter_side_run_wanted(_BP) is True
    assert documenter_side_run_wanted({**_BP, "docs": []}) is False
    assert documenter_side_run_wanted(None) is False


# --- review of #266: a re-dispatched architect must not leave a stale plan ------

def test_dispatching_the_architect_clears_the_previous_blueprint():
    from coordinare.graph.nodes.dispatch_performer import reset_blueprint_for_architect

    state = {"card_id": "c1", "blueprint": {"blueprint_hash": "h1", "milestones": [{"goal": "g"}]},
             "documenting_side": {"blueprint_hash": "h1", "status": "docs_committed"}}
    assert reset_blueprint_for_architect(state, "architecting") is True
    assert state["blueprint"] is None and state["documenting_side"] is None


@pytest.mark.parametrize("stage", ["implementing", "documenting", "qa", "reviewing", None])
def test_other_stages_keep_the_blueprint(stage):
    from coordinare.graph.nodes.dispatch_performer import reset_blueprint_for_architect

    bp = {"blueprint_hash": "h1"}
    state = {"card_id": "c1", "blueprint": bp, "documenting_side": None}
    assert reset_blueprint_for_architect(state, stage) is False
    assert state["blueprint"] is bp


def test_the_dispatch_body_resets_before_it_slices():
    """The reset has to run in the dispatch body, ahead of inject_briefs."""
    import inspect

    from coordinare.graph.nodes import dispatch_performer as dp

    src = inspect.getsource(dp._dispatch_performer_body)
    assert src.index("reset_blueprint_for_architect(state, performer_stage)") < src.index("inject_briefs(card_context, state, role=role)")
