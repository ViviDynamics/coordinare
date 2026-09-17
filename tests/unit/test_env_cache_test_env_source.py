"""Env-cache persistence of the agent-discovered ``test_env_source`` (spec 092, US2).

When no symphony-level ``test_env`` config block is set, the service-inference
agent may emit a path-only ``test_env_source`` on the manifest. Coordinare
persists that PATH alongside the env-cache fingerprint so a QA-runtime or
performer context that reuses the cache reloads the same file at runtime
instead of carrying baked-in values.

FR-017 (non-negotiable, carried from spec 091): the persisted env-cache state
stores only the PATH — never any loaded ``KEY=VALUE``. A reused cache reloads
variables from the source file, so no literal secret is ever written to disk.
"""

from __future__ import annotations

from pathlib import Path

from coordinare.daemon import _persist_env_cache
from coordinare.models.env_cache import EnvCacheState
from coordinare.state_store import EnvCacheStateSnapshot, WorkflowSnapshot


def _live_state(cache_dir: Path, **overrides) -> EnvCacheState:
    fields = {
        "symphony_name": "website",
        "sanitised_name": "website-a1b2c3",
        "cache_dir": cache_dir,
    }
    fields.update(overrides)
    return EnvCacheState(**fields)


def test_discovered_test_env_source_persists_into_snapshot(tmp_path: Path) -> None:
    # The live state carries the agent-discovered path; persisting it must copy
    # the PATH onto the durable snapshot so a restart can reload the same file.
    live = {"website": _live_state(tmp_path / "website", test_env_source=".coordinare/test.env")}
    snapshots = _persist_env_cache(live)
    assert snapshots["website"].test_env_source == ".coordinare/test.env"


def test_no_discovered_source_persists_as_none(tmp_path: Path) -> None:
    # No discovered path → the snapshot field is None (nothing to reload from).
    live = {"website": _live_state(tmp_path / "website")}
    snapshots = _persist_env_cache(live)
    assert snapshots["website"].test_env_source is None


def test_test_env_source_round_trips_through_workflow_snapshot(tmp_path: Path) -> None:
    # The path must survive a full WorkflowSnapshot serialise → deserialise so a
    # coordinare restart reloads it (the whole point of persisting it).
    snap = WorkflowSnapshot(
        snapshot_at=__import__("datetime").datetime(2026, 6, 16, tzinfo=None),
        phase="idle",
        env_cache={
            "website": EnvCacheStateSnapshot(
                symphony_name="website",
                sanitised_name="website-a1b2c3",
                cache_dir=str(tmp_path / "website"),
                test_env_source=".coordinare/test.env",
            ),
        },
    )
    restored = WorkflowSnapshot.model_validate_json(snap.model_dump_json())
    assert restored.env_cache["website"].test_env_source == ".coordinare/test.env"


def test_old_snapshot_without_field_loads_with_none() -> None:
    # Pre-092 snapshots have no ``test_env_source`` key. They must load (no
    # migration) with the field defaulting to None.
    snap = EnvCacheStateSnapshot(
        symphony_name="website",
        sanitised_name="website-a1b2c3",
        cache_dir="/devenv/website",
    )
    assert snap.test_env_source is None


def test_persisted_state_carries_only_path_never_values(tmp_path: Path) -> None:
    # FR-017: even though the live runtime loads literal KEY=VALUE test-env vars,
    # the DURABLE snapshot must carry only the path. Assert the serialised
    # snapshot exposes the path field and no field holding loaded values.
    live = {"website": _live_state(tmp_path / "website", test_env_source=".coordinare/test.env")}
    snapshots = _persist_env_cache(live)
    dumped = snapshots["website"].model_dump()
    assert dumped["test_env_source"] == ".coordinare/test.env"
    # No snapshot field should hold a dict of loaded variables (those are
    # secret-like and must never be persisted).
    assert not any(
        isinstance(v, dict) and v for v in dumped.values()
    ), "env-cache snapshot must not persist any loaded KEY=VALUE mapping"
