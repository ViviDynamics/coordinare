"""Spec 218 (rebase onto #147): the v36 step loads v35 snapshots with its defaults and keeps card_lifecycle.

#147 set schema v35 with the top-level ``card_lifecycle``. This branch's step is v36. A v35 snapshot that
carries ``card_lifecycle`` must load under v36 with the 218 defaults (no rendered scripts, no infra
budget), and the ``card_lifecycle`` it already had must survive the load unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from coordinare.state_store import CURRENT_SCHEMA_VERSION, EnvCacheStateSnapshot, WorkflowSnapshot


def _v35_snapshot(*, env_cache: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": 35,
        "snapshot_at": datetime.now(UTC).isoformat(),
        "phase": "idle",
        "active_sessions": {},
        "card_lifecycle": {
            "sym": {
                "CARD-1": [
                    {"kind": "dispatched", "stage": "implementing", "at": "2026-10-14T12:00:00+00:00"},
                ],
            },
        },
        "env_cache": env_cache or {},
    }


def test_the_current_schema_is_v37() -> None:
    # 234 (v37) moved the current version on from the 218 step; the v36 step's own defaults are still tested below.
    assert CURRENT_SCHEMA_VERSION == 38


def test_a_v35_snapshot_with_card_lifecycle_loads_under_v36() -> None:
    raw = _v35_snapshot()

    snapshot = WorkflowSnapshot.model_validate(raw)

    assert snapshot.card_lifecycle == raw["card_lifecycle"], "card_lifecycle must survive the load unchanged"


def test_a_v35_env_cache_entry_gets_the_218_defaults() -> None:
    """No rendered scripts and no infrastructure budget. Both fail closed, one re-bootstrap."""
    entry = {
        "symphony_name": "sym",
        "sanitised_name": "sym-abc",
        "cache_dir": "/tmp/sym-abc",
        "readme_sha": "abc",
        "last_bootstrap_succeeded": True,
        "cache_dir_ready": True,
    }

    snapshot = WorkflowSnapshot.model_validate(_v35_snapshot(env_cache={"sym": entry}))

    loaded = snapshot.env_cache["sym"]
    assert isinstance(loaded, EnvCacheStateSnapshot)
    assert loaded.rendered_verify_sh is None
    assert loaded.rendered_activate_sh is None
    assert loaded.infra_failures == 0
    assert loaded.last_infra_failure_at is None
    assert loaded.readme_sha == "abc"
    assert snapshot.card_lifecycle["sym"]["CARD-1"][0]["kind"] == "dispatched"
