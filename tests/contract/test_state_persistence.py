from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import jsonschema
import pytest

from coordinare.metrics import CoordinareMetrics
from coordinare.state_store import StateStore, WorkflowSnapshot

if TYPE_CHECKING:
    from pathlib import Path

# Resolve schema path relative to the repo root (two levels up from this file)
_SCHEMA_PATH = (
    __import__("pathlib").Path(__file__).resolve().parents[2]
    / "specs"
    / "003-state-persistence"
    / "contracts"
    / "workflow-snapshot.schema.json"
)


def _load_schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text())


def _make_snapshot(**overrides) -> WorkflowSnapshot:
    defaults = {
        "snapshot_at": datetime.now(UTC),
        "phase": "idle",
    }
    defaults.update(overrides)
    return WorkflowSnapshot(**defaults)


# --- T041: Validate StateStore.save() output against JSON Schema ---


@pytest.mark.asyncio
async def test_save_output_validates_against_schema_idle(tmp_path: Path) -> None:
    """Idle snapshot JSON validates against workflow-snapshot.schema.json."""
    schema = _load_schema()
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot(phase="idle")

    await store.save(snapshot)
    data = json.loads((tmp_path / "state.json").read_text())

    jsonschema.validate(instance=data, schema=schema)


@pytest.mark.asyncio
async def test_save_output_validates_against_schema_monitoring_agent(tmp_path: Path) -> None:
    """Monitoring_agent snapshot with all fields validates against schema."""
    schema = _load_schema()
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot(
        phase="monitoring_agent",
        active_card_id="PVT_CONTRACT",
        active_card_title="Contract Test Card",
        active_card_column="In Progress",
        agent_session_id="sess_contract",
    )

    await store.save(snapshot)
    data = json.loads((tmp_path / "state.json").read_text())

    jsonschema.validate(instance=data, schema=schema)


@pytest.mark.asyncio
async def test_save_output_validates_against_schema_monitoring_pr(tmp_path: Path) -> None:
    """Monitoring_pr snapshot with PR fields validates against schema."""
    schema = _load_schema()
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot(
        phase="monitoring_pr",
        active_card_id="PVT_PR",
        active_card_title="PR Card",
        active_card_column="In Review",
        pr_url="https://github.com/org/repo/pull/42",
        pr_node_id="PR_NODE_1",
    )

    await store.save(snapshot)
    data = json.loads((tmp_path / "state.json").read_text())

    jsonschema.validate(instance=data, schema=schema)


@pytest.mark.asyncio
async def test_save_output_validates_against_schema_blocked(tmp_path: Path) -> None:
    """Blocked snapshot with open_questions validates against schema."""
    schema = _load_schema()
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot(
        phase="blocked",
        active_card_id="PVT_BLOCKED",
        active_card_title="Blocked Card",
        active_card_column="Blocked",
        open_questions=["What API?", "Which provider?"],
    )

    await store.save(snapshot)
    data = json.loads((tmp_path / "state.json").read_text())

    jsonschema.validate(instance=data, schema=schema)


@pytest.mark.asyncio
async def test_save_output_has_required_fields(tmp_path: Path) -> None:
    """Saved JSON contains all required fields from the schema."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot(phase="idle")

    await store.save(snapshot)
    data = json.loads((tmp_path / "state.json").read_text())

    assert "schema_version" in data
    assert "snapshot_at" in data
    assert "phase" in data
    # v11 schema (096 last_known_main_sha + last_rebase_attempt) is current;
    # v1-v10 are still readable.
    assert data["schema_version"] == 11


@pytest.mark.asyncio
async def test_save_output_has_no_extra_fields(tmp_path: Path) -> None:
    """Saved JSON contains only fields defined in the schema (additionalProperties: false)."""
    schema = _load_schema()
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot(
        phase="monitoring_agent",
        active_card_id="PVT_EXTRA",
        active_card_title="Extra Fields Check",
        active_card_column="In Progress",
        agent_session_id="sess_extra",
        open_questions=["q1"],
    )

    await store.save(snapshot)
    data = json.loads((tmp_path / "state.json").read_text())

    allowed_keys = set(schema["properties"].keys())
    actual_keys = set(data.keys())
    extra = actual_keys - allowed_keys
    assert not extra, f"Unexpected fields in saved JSON: {extra}"
