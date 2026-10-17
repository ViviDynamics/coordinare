"""Schema 39 persists the env-cache bootstrap exhaustion reason."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest

from coordinare.metrics import CoordinareMetrics
from coordinare.state_store import (
    CURRENT_SCHEMA_VERSION,
    EnvCacheStateSnapshot,
    StateStore,
    WorkflowSnapshot,
)

PRE_EXHAUSTION_REASON_SCHEMA_VERSION = 38
EXHAUSTION_REASON_SCHEMA_VERSION = 39


def test_schema_version_records_persisted_exhaustion_reason() -> None:
    assert CURRENT_SCHEMA_VERSION == 40
    assert EXHAUSTION_REASON_SCHEMA_VERSION in range(1, CURRENT_SCHEMA_VERSION + 1)


@pytest.mark.parametrize("reason", [None, "configuration_refusal"])
def test_v39_exhaustion_reason_matches_json_schema(reason: str | None) -> None:
    schema = json.loads(Path("specs/003-state-persistence/contracts/workflow-snapshot.schema.json").read_text())
    entry = EnvCacheStateSnapshot(
        symphony_name="sym", sanitised_name="sym-abc", cache_dir="/devenv/sym-abc",
        bootstrap_exhaustion_reason=reason,
    )
    snapshot = WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="idle", env_cache={"sym": entry})

    jsonschema.validate(instance=snapshot.model_dump(mode="json"), schema=schema)
    properties = schema["properties"]["env_cache"]["additionalProperties"]["properties"]
    assert properties["bootstrap_exhaustion_reason"]["default"] is None


@pytest.mark.asyncio
async def test_v38_snapshot_without_exhaustion_reason_loads_with_default(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": PRE_EXHAUSTION_REASON_SCHEMA_VERSION,
        "snapshot_at": "2026-10-15T12:00:00+00:00",
        "phase": "idle",
        "env_cache": {"sym": {
            "symphony_name": "sym",
            "sanitised_name": "sym-abc",
            "cache_dir": "/devenv/sym-abc",
            "bootstrap_exhausted": True,
            "bootstrap_commit_sha": "abc",
        }},
    }))

    snapshot = await StateStore(path=path, metrics=CoordinareMetrics()).load()

    assert snapshot is not None
    assert snapshot.schema_version == PRE_EXHAUSTION_REASON_SCHEMA_VERSION
    entry = snapshot.env_cache["sym"]
    assert entry.bootstrap_exhaustion_reason is None
    assert entry.bootstrap_exhausted
    assert entry.bootstrap_commit_sha == "abc"
