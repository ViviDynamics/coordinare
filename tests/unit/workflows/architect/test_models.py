"""165 data-model.md: every bound on the blueprint is enforced at the boundary.

A bound that is not tested is a bound that drifts. One test per bound,
checking the last accepted value and the first rejected one.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from performer.workflows.architect.models import (
    Blueprint,
    Criterion,
    DataModel,
    DataModelChange,
    DocTopic,
    Interface,
    Milestone,
    Module,
)
from pydantic import ValidationError


def _ms(n: int) -> list[dict]:
    return [{"goal": f"g{i}", "scope": ["a"], "done_when": "d"} for i in range(n)]


def _crit(n: int) -> list[dict]:
    return [{"surface": "/", "action": "open", "expected": "ok", "kind": "functional"} for _ in range(n)]


def _bp(**over) -> dict:
    base = {
        "summary": "s",
        "milestones": _ms(1),
        "modules": [],
        "data_model": {"changes": []},
        "interfaces": [],
        "risks": [],
        "criteria": _crit(1),
        "docs": [],
    }
    base.update(over)
    return base


def test_minimal_blueprint_validates():
    bp = Blueprint.model_validate(_bp())
    assert bp.milestones[0].goal == "g0" and bp.docs == []


@pytest.mark.parametrize("n,ok", [(1, True), (7, True), (0, False), (8, False)])
def test_milestone_count_bounds(n, ok):
    data = _bp(milestones=_ms(n))
    if ok:
        Blueprint.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            Blueprint.model_validate(data)


@pytest.mark.parametrize("n,ok", [(0, True), (1, True), (12, True), (13, False)])
def test_criteria_count_bounds(n, ok):
    data = _bp(criteria=_crit(n))
    if ok:
        Blueprint.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            Blueprint.model_validate(data)


@pytest.mark.parametrize("field,n,ok", [
    ("modules", 12, True), ("modules", 13, False),
    ("interfaces", 12, True), ("interfaces", 13, False),
    ("risks", 8, True), ("risks", 9, False),
    ("docs", 8, True), ("docs", 9, False),
])
def test_other_list_bounds(field, n, ok):
    items = {
        "modules": [{"path": "app/", "note": "n"}] * n,
        "interfaces": [{"name": "i", "kind": "class", "contract": "c"}] * n,
        "risks": ["r"] * n,
        "docs": [{"topic": "t", "location": "l", "say": "s"}] * n,
    }[field]
    data = _bp(**{field: items})
    if ok:
        Blueprint.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            Blueprint.model_validate(data)


def test_data_model_changes_bounded_and_kind_constrained():
    ok = {"changes": [{"kind": "column", "name": "c", "note": "n"}] * 12}
    DataModel.model_validate(ok)
    with pytest.raises(ValidationError):
        DataModel.model_validate({"changes": [{"kind": "column", "name": "c", "note": "n"}] * 13})
    with pytest.raises(ValidationError):
        DataModelChange.model_validate({"kind": "spreadsheet", "name": "c", "note": "n"})


@pytest.mark.parametrize("model,field,limit", [
    (Milestone, "goal", 200), (Milestone, "done_when", 300),
    (Module, "path", 120), (Module, "note", 200),
    (Interface, "contract", 300),
    (Criterion, "surface", 120), (Criterion, "action", 200), (Criterion, "expected", 300),
    (DocTopic, "topic", 120), (DocTopic, "location", 160), (DocTopic, "say", 400),
])
def test_string_bounds(model, field, limit):
    base = {
        Milestone: {"goal": "g", "scope": ["a"], "done_when": "d"},
        Module: {"path": "p", "note": "n"},
        Interface: {"name": "n", "kind": "endpoint", "contract": "c"},
        Criterion: {"surface": "/", "action": "a", "expected": "e", "kind": "visual"},
        DocTopic: {"topic": "t", "location": "l", "say": "s"},
    }[model]
    model.model_validate({**base, field: "x" * limit})
    with pytest.raises(ValidationError):
        model.model_validate({**base, field: "x" * (limit + 1)})


def test_summary_and_risk_string_bounds():
    Blueprint.model_validate(_bp(summary="x" * 600))
    with pytest.raises(ValidationError):
        Blueprint.model_validate(_bp(summary="x" * 601))
    Blueprint.model_validate(_bp(risks=["x" * 300]))
    with pytest.raises(ValidationError):
        Blueprint.model_validate(_bp(risks=["x" * 301]))


def test_milestone_scope_bounded_to_eight_entries():
    Milestone.model_validate({"goal": "g", "scope": ["a"] * 8, "done_when": "d"})
    with pytest.raises(ValidationError):
        Milestone.model_validate({"goal": "g", "scope": ["a"] * 9, "done_when": "d"})


def test_unknown_fields_are_rejected_everywhere():
    with pytest.raises(ValidationError):
        Blueprint.model_validate(_bp(size="small"))  # size is set by code, not the model
    with pytest.raises(ValidationError):
        Milestone.model_validate({"goal": "g", "scope": [], "done_when": "d", "owner": "me"})


def test_kind_enums():
    for kind in ("functional", "visual", "command"):
        Criterion.model_validate({"surface": "/", "action": "a", "expected": "e", "kind": kind})
    with pytest.raises(ValidationError):
        Criterion.model_validate({"surface": "/", "action": "a", "expected": "e", "kind": "vibes"})
    for kind in ("endpoint", "class", "event", "cli"):
        Interface.model_validate({"name": "n", "kind": kind, "contract": "c"})
    with pytest.raises(ValidationError):
        Interface.model_validate({"name": "n", "kind": "thing", "contract": "c"})


def test_rendered_schema_matches_the_contract_fixture_field_for_field():
    """The schema the model is shown (schema_guard.render_schema) must carry the
    same fields, requireds, enums and bounds as the committed contract."""
    from performer.workflows.schema_guard import render_schema

    contract = json.loads((Path(__file__).parent / "fixtures" / "blueprint.schema.json").read_text())
    rendered = json.loads(render_schema(Blueprint)) if isinstance(render_schema(Blueprint), str) else render_schema(Blueprint)

    def walk(c, r, path=""):
        assert set(c.get("required", [])) == set(r.get("required", [])), f"{path} required"
        assert set(c.get("properties", {})) == set(r.get("properties", {})), f"{path} properties"
        for name, cprop in c.get("properties", {}).items():
            rprop = r["properties"][name]
            for bound in ("maxLength", "minItems", "maxItems"):
                if bound in cprop:
                    assert rprop.get(bound) == cprop[bound], f"{path}.{name} {bound}"
            if "enum" in cprop:
                assert set(rprop.get("enum", [])) == set(cprop["enum"]), f"{path}.{name} enum"
            if cprop.get("type") == "array" and isinstance(cprop.get("items"), dict) and "properties" in cprop["items"]:
                walk(cprop["items"], _resolve(rprop["items"], rendered), f"{path}.{name}[]")
            elif "properties" in cprop:
                walk(cprop, _resolve(rprop, rendered), f"{path}.{name}")

    def _resolve(node, root):
        if "$ref" in node:
            return root["$defs"][node["$ref"].split("/")[-1]]
        return node

    walk(contract, rendered)
