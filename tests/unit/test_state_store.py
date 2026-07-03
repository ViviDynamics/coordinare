from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from coordinare.metrics import CoordinareMetrics
from coordinare.state_store import (
    CURRENT_SCHEMA_VERSION,
    EnvCacheStateSnapshot,
    PersistedSession,
    StateLoadError,
    StateStore,
    WorkflowSnapshot,
)


def _make_snapshot(**overrides) -> WorkflowSnapshot:
    defaults = {
        "snapshot_at": datetime.now(UTC),
        "phase": "idle",
    }
    defaults.update(overrides)
    return WorkflowSnapshot(**defaults)


# --- T010: save/load round-trip tests ---


@pytest.mark.asyncio
async def test_save_load_round_trip_idle(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot(phase="idle")

    await store.save(snapshot)
    loaded = await store.load()

    assert loaded is not None
    assert loaded.phase == "idle"
    assert loaded.active_card_id is None
    assert loaded.schema_version == CURRENT_SCHEMA_VERSION


@pytest.mark.asyncio
async def test_save_load_round_trip_monitoring_agent(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot(
        phase="monitoring_agent",
        active_card_id="PVT_1",
        active_card_title="Test Card",
        active_card_column="In Progress",
        agent_session_id="sess_abc",
    )

    await store.save(snapshot)
    loaded = await store.load()

    assert loaded is not None
    assert loaded.phase == "monitoring_agent"
    assert loaded.active_card_id == "PVT_1"
    assert loaded.active_card_title == "Test Card"
    assert loaded.agent_session_id == "sess_abc"


@pytest.mark.asyncio
async def test_save_load_round_trip_blocked(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot(
        phase="blocked",
        active_card_id="PVT_2",
        active_card_title="Blocked Card",
        active_card_column="Blocked",
        open_questions=["What API?", "Which provider?"],
    )

    await store.save(snapshot)
    loaded = await store.load()

    assert loaded is not None
    assert loaded.phase == "blocked"
    assert loaded.open_questions == ["What API?", "Which provider?"]


@pytest.mark.asyncio
async def test_save_load_round_trip_monitoring_pr(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot(
        phase="monitoring_pr",
        active_card_id="PVT_3",
        active_card_title="PR Card",
        active_card_column="In Review",
        pr_url="https://github.com/org/repo/pull/42",
        pr_node_id="PR_NODE_1",
    )

    await store.save(snapshot)
    loaded = await store.load()

    assert loaded is not None
    assert loaded.phase == "monitoring_pr"
    assert loaded.pr_url == "https://github.com/org/repo/pull/42"
    assert loaded.pr_node_id == "PR_NODE_1"


# --- 045: card metadata (issue_number etc.) persists across save/load ---


@pytest.mark.asyncio
async def test_save_load_round_trip_card_issue_metadata(tmp_path: Path) -> None:
    """045: issue_number, issue_url, description, and acceptance_criteria
    must round-trip so restart doesn't dispatch with issue_number=0 (which
    previously produced PRs with null bodies — no ``Closes #N`` linkage)."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot(
        phase="monitoring_agent",
        active_card_id="PVT_4",
        active_card_title="Linked Card",
        active_card_column="In Progress",
        active_card_issue_id="I_kwDO_issue",
        active_card_issue_number=89,
        active_card_issue_url="https://github.com/org/repo/issues/89",
        active_card_description="Fix the bug.",
        active_card_acceptance_criteria=["Tests pass", "Lint clean"],
    )

    await store.save(snapshot)
    loaded = await store.load()

    assert loaded is not None
    assert loaded.active_card_issue_number == 89
    assert loaded.active_card_issue_url == "https://github.com/org/repo/issues/89"
    assert loaded.active_card_description == "Fix the bug."
    assert loaded.active_card_acceptance_criteria == ["Tests pass", "Lint clean"]


def test_snapshot_rejects_non_positive_issue_number() -> None:
    """045: ``active_card_issue_number`` must be >= 1 to match the JSON-schema
    contract.  A snapshot that somehow contains ``0`` or a negative value
    must fail validation loudly rather than round-tripping and
    re-introducing the null-PR-body bug.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _make_snapshot(active_card_issue_number=0)
    with pytest.raises(ValidationError):
        _make_snapshot(active_card_issue_number=-5)
    # None remains valid (idle / unknown).
    snap = _make_snapshot(active_card_issue_number=None)
    assert snap.active_card_issue_number is None


# --- T011: load returns None when absent; verify_writable passes ---


@pytest.mark.asyncio
async def test_load_returns_none_when_file_absent(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "missing.json", metrics=metrics)

    result = await store.load()

    assert result is None


def test_verify_writable_passes_for_writable_dir(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)

    store.verify_writable()  # Should not raise


# --- T012: metrics recorded on successful save ---


@pytest.mark.asyncio
async def test_save_records_metrics(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot()

    await store.save(snapshot)

    # Histogram should have one observation
    assert metrics.state_write_duration_seconds._sum.get() > 0
    # Gauge should be set to snapshot timestamp
    assert metrics.state_last_written_timestamp._value.get() > 0


# --- T028: corrupt JSON ---


@pytest.mark.asyncio
async def test_load_corrupt_json_raises_state_load_error(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    path = tmp_path / "state.json"
    path.write_text("not valid json {{{")
    store = StateStore(path=path, metrics=metrics)

    with pytest.raises(StateLoadError) as exc_info:
        await store.load()

    assert exc_info.value.reason == "corrupt"


# --- T029: schema version mismatch ---


@pytest.mark.asyncio
async def test_load_schema_mismatch_raises_state_load_error(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    path = tmp_path / "state.json"
    store = StateStore(path=path, metrics=metrics)
    snapshot = _make_snapshot()
    await store.save(snapshot)

    # Manually tamper with schema_version
    import json

    data = json.loads(path.read_text())
    data["schema_version"] = 999
    path.write_text(json.dumps(data))

    with pytest.raises(StateLoadError) as exc_info:
        await store.load()

    assert exc_info.value.reason == "schema_mismatch"


# --- T030: invalid phase value ---


@pytest.mark.asyncio
async def test_load_invalid_phase_raises_state_load_error(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    path = tmp_path / "state.json"
    store = StateStore(path=path, metrics=metrics)
    snapshot = _make_snapshot()
    await store.save(snapshot)

    import json

    data = json.loads(path.read_text())
    data["phase"] = "totally_invalid_phase"
    path.write_text(json.dumps(data))

    with pytest.raises(StateLoadError) as exc_info:
        await store.load()

    assert exc_info.value.reason == "corrupt"


# --- T031: disk full simulation (save catches OSError) ---


@pytest.mark.asyncio
async def test_save_catches_os_error_on_disk_full(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot()

    with patch("coordinare.state_store.os.replace", side_effect=OSError("disk full")):
        await store.save(snapshot)  # Should not raise

    assert metrics.state_write_failures_total._value.get() == 1.0


# --- T032: torn write protection (original file survives) ---


@pytest.mark.asyncio
async def test_torn_write_preserves_original_file(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    path = tmp_path / "state.json"
    store = StateStore(path=path, metrics=metrics)

    # Write a valid first snapshot
    first = _make_snapshot(phase="idle")
    await store.save(first)
    original_content = path.read_text()

    # Attempt a second write that fails at os.replace
    second = _make_snapshot(phase="monitoring_agent", active_card_id="PVT_X")
    with patch("coordinare.state_store.os.replace", side_effect=OSError("disk full")):
        await store.save(second)

    # Original file should still be intact
    assert path.read_text() == original_content


# --- T033: verify_writable raises on read-only dir ---


def test_verify_writable_raises_on_read_only_dir(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    read_only = tmp_path / "readonly"
    read_only.mkdir()
    os.chmod(read_only, 0o444)

    store = StateStore(path=read_only / "state.json", metrics=metrics)

    try:
        with pytest.raises(OSError):
            store.verify_writable()
    finally:
        os.chmod(read_only, 0o755)


# --- load() OSError on read_bytes → StateLoadError ---


@pytest.mark.asyncio
async def test_load_oserror_on_read_bytes_raises_state_load_error(tmp_path: Path) -> None:
    """StateStore.load() raises StateLoadError(reason='corrupt') when read_bytes() raises OSError."""

    metrics = CoordinareMetrics()
    path = tmp_path / "state.json"
    # Create the file so that exists() returns True
    path.write_bytes(b"{}")

    store = StateStore(path=path, metrics=metrics)

    with patch("pathlib.Path.read_bytes", side_effect=OSError("permission denied")), pytest.raises(StateLoadError) as exc_info:
        await store.load()

    assert exc_info.value.reason == "corrupt"


# --- T039: performance benchmark ---


@pytest.mark.asyncio
async def test_save_completes_within_budget(tmp_path: Path) -> None:
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot(
        phase="monitoring_agent",
        active_card_id="PVT_PERF",
        active_card_title="Perf Test Card",
        active_card_column="In Progress",
        agent_session_id="sess_perf",
        open_questions=["q1", "q2", "q3"],
    )

    start = time.monotonic()
    await store.save(snapshot)
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"State write took {elapsed:.3f}s, exceeding 1.0s budget (SC-002)"


# --- FR-007: card_clarifications and monitoring_pr phase survive save/load ---


@pytest.mark.asyncio
async def test_state_store_preserves_card_clarifications_and_monitoring_pr_phase(
    tmp_path: Path,
) -> None:
    """WorkflowSnapshot with phase='monitoring_pr' and card_clarifications round-trips correctly."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    clarifications = [
        {"questions": ["What API?", "Which region?"], "answer": ""},
        {"questions": ["Deadline?"], "answer": "End of sprint"},
    ]
    snapshot = _make_snapshot(
        phase="monitoring_pr",
        active_card_id="PVT_RTRIP",
        active_card_title="Round Trip Card",
        active_card_column="In Review",
        card_clarifications=clarifications,
    )

    await store.save(snapshot)
    loaded = await store.load()

    assert loaded is not None
    assert loaded.phase == "monitoring_pr"
    assert loaded.card_clarifications == clarifications


# --- 065 Fix 7b: active_sessions persistence (multi-card stage survival) ---


@pytest.mark.asyncio
async def test_save_load_round_trip_active_sessions(tmp_path: Path) -> None:
    """PersistedSession entries round-trip so a restart in multi-card mode does
    not demote a closing_review card back to implementing."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    sessions = {
        "PVT_A": PersistedSession(
            card_id="PVT_A",
            performer_stage="closing_review",
            phase="monitoring_performer",
            processed_review_ids=["r1", "r2"],
        ),
        "PVT_B": PersistedSession(
            card_id="PVT_B",
            performer_stage="implementing",
            phase="monitoring_agent",
        ),
    }
    snapshot = _make_snapshot(
        phase="monitoring_performer",
        active_card_id="PVT_A",
        active_sessions=sessions,
    )

    await store.save(snapshot)
    loaded = await store.load()

    assert loaded is not None
    assert set(loaded.active_sessions.keys()) == {"PVT_A", "PVT_B"}
    assert loaded.active_sessions["PVT_A"].performer_stage == "closing_review"
    assert loaded.active_sessions["PVT_A"].processed_review_ids == ["r1", "r2"]
    assert loaded.active_sessions["PVT_B"].performer_stage == "implementing"


@pytest.mark.asyncio
async def test_load_v1_snapshot_forward_compat(tmp_path: Path) -> None:
    """A v1 snapshot (no active_sessions field) loads cleanly under v2 with an
    empty active_sessions dict — board re-adopt fills it back in."""
    import json

    metrics = CoordinareMetrics()
    path = tmp_path / "state.json"
    store = StateStore(path=path, metrics=metrics)
    snapshot = _make_snapshot(phase="idle")
    await store.save(snapshot)

    data = json.loads(path.read_text())
    data["schema_version"] = 1
    data.pop("active_sessions", None)
    path.write_text(json.dumps(data))

    loaded = await store.load()
    assert loaded is not None
    assert loaded.active_sessions == {}


# --- 069 T005/T006: last_blocked_notified_at on PersistedSession ---


@pytest.mark.asyncio
async def test_persisted_session_roundtrips_last_blocked_notified_at(tmp_path: Path) -> None:
    """069 T005 / FR-001: a snapshot's per-card ``last_blocked_notified_at``
    survives a save → load round-trip so the dedup gate holds across restart.
    """
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    t_blocked = datetime(2026, 5, 21, 21, 32, 4, tzinfo=UTC)
    snapshot = _make_snapshot(
        phase="blocked",
        active_card_id="PVT_70",
        active_sessions={
            "PVT_70": PersistedSession(
                card_id="PVT_70",
                performer_stage="implementing",
                phase="blocked",
                last_blocked_notified_at=t_blocked,
            ),
        },
    )

    await store.save(snapshot)
    loaded = await store.load()

    assert loaded is not None
    assert loaded.active_sessions["PVT_70"].last_blocked_notified_at == t_blocked


@pytest.mark.asyncio
async def test_v1_snapshot_defaults_last_blocked_notified_at_to_none(tmp_path: Path) -> None:
    """069 T006: a v1 snapshot whose ``PersistedSession`` entries lack the new
    field deserializes cleanly with ``last_blocked_notified_at == None``.
    """
    import json

    metrics = CoordinareMetrics()
    path = tmp_path / "state.json"
    store = StateStore(path=path, metrics=metrics)
    snapshot = _make_snapshot(
        phase="blocked",
        active_card_id="PVT_70",
        active_sessions={
            "PVT_70": PersistedSession(card_id="PVT_70", performer_stage="implementing"),
        },
    )
    await store.save(snapshot)

    data = json.loads(path.read_text())
    data["schema_version"] = 1
    data["active_sessions"]["PVT_70"].pop("last_blocked_notified_at", None)
    data["active_sessions"]["PVT_70"].pop("last_blocked_slack_delivered_at", None)
    path.write_text(json.dumps(data))

    loaded = await store.load()
    assert loaded is not None
    assert loaded.active_sessions["PVT_70"].last_blocked_notified_at is None
    assert loaded.active_sessions["PVT_70"].last_blocked_slack_delivered_at is None


@pytest.mark.asyncio
async def test_persisted_session_roundtrips_last_blocked_slack_delivered_at(tmp_path: Path) -> None:
    """069 FR-004: ``last_blocked_slack_delivered_at`` survives a save → load
    round-trip so the post-restart Slack-dedup gate holds.  This is a
    separate field from ``last_blocked_notified_at`` because the latter is
    rewritten on every handle_blocked pass and cannot prove Slack fired.
    """
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    t_slack = datetime(2026, 5, 23, 16, 30, 0, tzinfo=UTC)
    snapshot = _make_snapshot(
        phase="blocked",
        active_card_id="PVT_70",
        active_sessions={
            "PVT_70": PersistedSession(
                card_id="PVT_70",
                performer_stage="implementing",
                phase="blocked",
                last_blocked_slack_delivered_at=t_slack,
            ),
        },
    )

    await store.save(snapshot)
    loaded = await store.load()

    assert loaded is not None
    assert loaded.active_sessions["PVT_70"].last_blocked_slack_delivered_at == t_slack


# --- 072 T013: head_at_dispatch / head_at_last_turn on PersistedSession ---


@pytest.mark.asyncio
async def test_persisted_session_roundtrips_head_delta_fields(tmp_path: Path) -> None:
    """072 FR-072-8..10: ``head_at_dispatch`` and ``head_at_last_turn`` survive
    a save → load round-trip so the audit trail holds across restart.
    """
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot(
        phase="dispatching",
        active_card_id="PVT_70",
        active_sessions={
            "PVT_70": PersistedSession(
                card_id="PVT_70",
                performer_stage="implementing",
                phase="dispatching",
                head_at_dispatch="abc1234",
                head_at_last_turn="def5678",
            ),
        },
    )

    await store.save(snapshot)
    loaded = await store.load()

    assert loaded is not None
    assert loaded.active_sessions["PVT_70"].head_at_dispatch == "abc1234"
    assert loaded.active_sessions["PVT_70"].head_at_last_turn == "def5678"


@pytest.mark.asyncio
async def test_v1_snapshot_defaults_head_delta_fields_to_none(tmp_path: Path) -> None:
    """072 FR-072-8: a pre-072 snapshot whose ``PersistedSession`` entries lack
    the new head fields deserializes cleanly with both defaulting to ``None``.
    """
    import json

    metrics = CoordinareMetrics()
    path = tmp_path / "state.json"
    store = StateStore(path=path, metrics=metrics)
    snapshot = _make_snapshot(
        phase="dispatching",
        active_card_id="PVT_70",
        active_sessions={
            "PVT_70": PersistedSession(card_id="PVT_70", performer_stage="implementing"),
        },
    )
    await store.save(snapshot)

    data = json.loads(path.read_text())
    data["schema_version"] = 1
    data["active_sessions"]["PVT_70"].pop("head_at_dispatch", None)
    data["active_sessions"]["PVT_70"].pop("head_at_last_turn", None)
    path.write_text(json.dumps(data))

    loaded = await store.load()
    assert loaded is not None
    assert loaded.active_sessions["PVT_70"].head_at_dispatch is None
    assert loaded.active_sessions["PVT_70"].head_at_last_turn is None


# --- 073 Fix 3: env_cache persistence (avoid re-bootstrap on restart) ---


@pytest.mark.asyncio
async def test_env_cache_roundtrips_readme_sha(tmp_path: Path) -> None:
    """073 Fix 3: ``env_cache`` survives save → load so a restart with unchanged
    env-spec files skips the expensive env_bootstrap performer job."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    t_boot = datetime(2026, 5, 25, 12, 0, 0, tzinfo=UTC)
    snapshot = _make_snapshot(
        env_cache={
            "sym-one": EnvCacheStateSnapshot(
                symphony_name="sym-one",
                sanitised_name="sym-one-a1b2c3",
                cache_dir="/devenv/sym-one",
                readme_sha="deadbeef",
                last_bootstrap_at=t_boot,
                last_bootstrap_succeeded=True,
                cache_dir_ready=True,
            ),
        },
    )

    await store.save(snapshot)
    loaded = await store.load()

    assert loaded is not None
    assert "sym-one" in loaded.env_cache
    entry = loaded.env_cache["sym-one"]
    assert entry.readme_sha == "deadbeef"
    assert entry.last_bootstrap_at == t_boot
    assert entry.last_bootstrap_succeeded is True
    assert entry.cache_dir_ready is True
    assert entry.cache_dir == "/devenv/sym-one"


@pytest.mark.asyncio
async def test_v2_snapshot_defaults_env_cache_to_empty(tmp_path: Path) -> None:
    """073 Fix 3: a v2 snapshot (no ``env_cache`` field) loads cleanly under v3
    with an empty ``env_cache`` dict — the first poll cycle re-fetches the SHA.
    """
    import json

    metrics = CoordinareMetrics()
    path = tmp_path / "state.json"
    store = StateStore(path=path, metrics=metrics)
    snapshot = _make_snapshot(phase="idle")
    await store.save(snapshot)

    data = json.loads(path.read_text())
    data["schema_version"] = 2
    data.pop("env_cache", None)
    path.write_text(json.dumps(data))

    loaded = await store.load()
    assert loaded is not None
    assert loaded.env_cache == {}


@pytest.mark.asyncio
async def test_v1_snapshot_defaults_env_cache_to_empty(tmp_path: Path) -> None:
    """073 Fix 3 / review callout: a v1 snapshot (predates both
    ``active_sessions`` and ``env_cache``) must round-trip into v3 with an
    empty ``env_cache`` dict — MIN_SUPPORTED_SCHEMA_VERSION is 1, so v1
    snapshots must load without raising, and the first poll cycle then
    re-fetches the SHA. Complements the v2 case above; together they cover
    the full graceful-upgrade matrix the PR body claims.
    """
    import json

    metrics = CoordinareMetrics()
    path = tmp_path / "state.json"
    store = StateStore(path=path, metrics=metrics)
    snapshot = _make_snapshot(phase="idle")
    await store.save(snapshot)

    data = json.loads(path.read_text())
    data["schema_version"] = 1
    # v1 had neither active_sessions nor env_cache.
    data.pop("active_sessions", None)
    data.pop("env_cache", None)
    path.write_text(json.dumps(data))

    loaded = await store.load()
    assert loaded is not None
    assert loaded.env_cache == {}
    assert loaded.active_sessions == {}


@pytest.mark.asyncio
async def test_env_cache_snapshot_omits_transient_fields(tmp_path: Path) -> None:
    """073 Fix 3: the persisted shape MUST NOT include transient fields
    (``bootstrap_in_flight``, ``pending_sha``, ``runtime_health_failed``) so a
    crash mid-bootstrap does not leave a stuck flag on disk after restart.
    """
    import json

    metrics = CoordinareMetrics()
    path = tmp_path / "state.json"
    store = StateStore(path=path, metrics=metrics)
    snapshot = _make_snapshot(
        env_cache={
            "sym-one": EnvCacheStateSnapshot(
                symphony_name="sym-one",
                sanitised_name="sym-one-a1b2c3",
                cache_dir="/devenv/sym-one",
                readme_sha="abc123",
                cache_dir_ready=True,
            ),
        },
    )
    await store.save(snapshot)

    data = json.loads(path.read_text())
    persisted = data["env_cache"]["sym-one"]
    assert "bootstrap_in_flight" not in persisted
    assert "pending_sha" not in persisted
    assert "runtime_health_failed" not in persisted


# --- 074 FR-011: persona_scope persistence (graceful upgrade across restart) ---


@pytest.mark.asyncio
async def test_v3_snapshot_loads_with_null_persona_scope(tmp_path: Path) -> None:
    """074 FR-011: a v3 snapshot whose PersistedSession entries lack
    ``persona_scope`` deserializes cleanly under v4 with ``persona_scope = None``;
    the next cycle's classify_scope node recomputes from scratch.
    """
    import json

    metrics = CoordinareMetrics()
    path = tmp_path / "state.json"
    store = StateStore(path=path, metrics=metrics)
    snapshot = _make_snapshot(
        phase="dispatching",
        active_card_id="PVT_74",
        active_sessions={
            "PVT_74": PersistedSession(card_id="PVT_74", performer_stage="implementing"),
        },
    )
    await store.save(snapshot)

    data = json.loads(path.read_text())
    data["schema_version"] = 3
    data["active_sessions"]["PVT_74"].pop("persona_scope", None)
    path.write_text(json.dumps(data))

    loaded = await store.load()
    assert loaded is not None
    assert loaded.active_sessions["PVT_74"].persona_scope is None


@pytest.mark.asyncio
async def test_v4_snapshot_roundtrips_persona_scope(tmp_path: Path) -> None:
    """074 FR-011: a populated ``PersonaScope`` survives save → load with deep
    equality so the previous-cycle fallback chain in FR-006 holds across restart.
    """
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    scope = {
        "computed_at": "2026-05-27T18:30:00Z",
        "cycle_index": 7,
        "classifier_model": "anthropic_api:claude-haiku-4-5",
        "head_sha": "abc123",
        "files_summary": [{"path": "README.md", "added": 3, "removed": 1, "classes": ["docs"]}],
        "personas": {
            "reviewer": {"depth": "skim", "focus": "spot-check", "overrides": []},
            "security": {"depth": "skip", "focus": "no security paths", "overrides": []},
        },
    }
    snapshot = _make_snapshot(
        phase="dispatching",
        active_card_id="PVT_74",
        active_sessions={
            "PVT_74": PersistedSession(
                card_id="PVT_74",
                performer_stage="implementing",
                persona_scope=scope,
            ),
        },
    )

    await store.save(snapshot)
    loaded = await store.load()

    assert loaded is not None
    assert loaded.active_sessions["PVT_74"].persona_scope == scope


# --- 075: bounce_counter persistence (v4 → v5 graceful upgrade) ---


@pytest.mark.asyncio
async def test_v4_snapshot_loads_with_empty_bounce_counter(tmp_path: Path) -> None:
    """075: a v4 snapshot whose PersistedSession entries lack
    ``bounce_counter`` deserializes cleanly under v5 with ``bounce_counter = {}``;
    the next gate decision repopulates the head SHA entry.
    """
    import json

    metrics = CoordinareMetrics()
    path = tmp_path / "state.json"
    store = StateStore(path=path, metrics=metrics)
    snapshot = _make_snapshot(
        phase="dispatching",
        active_card_id="PVT_75",
        active_sessions={
            "PVT_75": PersistedSession(card_id="PVT_75", performer_stage="implementing"),
        },
    )
    await store.save(snapshot)

    data = json.loads(path.read_text())
    data["schema_version"] = 4
    data["active_sessions"]["PVT_75"].pop("bounce_counter", None)
    path.write_text(json.dumps(data))

    loaded = await store.load()
    assert loaded is not None
    assert loaded.active_sessions["PVT_75"].bounce_counter == {}


@pytest.mark.asyncio
async def test_v5_snapshot_roundtrips_bounce_counter(tmp_path: Path) -> None:
    """075: per-HEAD bounce counts survive save → load so the escalation gate
    does not forget bounces across daemon restarts.
    """
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot(
        phase="monitoring_performer",
        active_card_id="PVT_75",
        active_sessions={
            "PVT_75": PersistedSession(
                card_id="PVT_75",
                performer_stage="implementing",
                bounce_counter={"sha-abc": 2, "sha-def": 1},
            ),
        },
    )

    await store.save(snapshot)
    loaded = await store.load()

    assert loaded is not None
    assert loaded.active_sessions["PVT_75"].bounce_counter == {"sha-abc": 2, "sha-def": 1}


# --- 089: local_fix_counter persistence (v7 → v8 graceful upgrade) ---


@pytest.mark.asyncio
async def test_v7_snapshot_loads_with_empty_local_fix_counter(tmp_path: Path) -> None:
    """089: a pre-089 snapshot (schema v7) whose PersistedSession entries lack
    ``local_fix_counter`` deserializes cleanly under v8 with
    ``local_fix_counter == {}``; the next local-test-gate decision repopulates
    the head SHA entry.
    """
    import json

    metrics = CoordinareMetrics()
    path = tmp_path / "state.json"
    store = StateStore(path=path, metrics=metrics)
    snapshot = _make_snapshot(
        phase="dispatching",
        active_card_id="PVT_89",
        active_sessions={
            "PVT_89": PersistedSession(card_id="PVT_89", performer_stage="implementing"),
        },
    )
    await store.save(snapshot)

    data = json.loads(path.read_text())
    data["schema_version"] = 7
    data["active_sessions"]["PVT_89"].pop("local_fix_counter", None)
    path.write_text(json.dumps(data))

    loaded = await store.load()
    assert loaded is not None
    assert loaded.active_sessions["PVT_89"].local_fix_counter == {}


@pytest.mark.asyncio
async def test_v8_snapshot_roundtrips_local_fix_counter(tmp_path: Path) -> None:
    """089: per-HEAD local-fix counts survive save → load so the escalation gate
    does not forget local-test-gate fix attempts across daemon restarts. The
    counter is independent of ``bounce_counter`` (SC-004)."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot(
        phase="monitoring_performer",
        active_card_id="PVT_89",
        active_sessions={
            "PVT_89": PersistedSession(
                card_id="PVT_89",
                performer_stage="implementing",
                bounce_counter={"sha-abc": 1},
                local_fix_counter={"sha-abc": 2, "sha-def": 1},
            ),
        },
    )

    await store.save(snapshot)
    loaded = await store.load()

    assert loaded is not None
    session = loaded.active_sessions["PVT_89"]
    assert session.local_fix_counter == {"sha-abc": 2, "sha-def": 1}
    # SC-004: the two counters round-trip independently.
    assert session.bounce_counter == {"sha-abc": 1}


@pytest.mark.asyncio
async def test_env_cache_roundtrips_bootstrap_budget_fields(tmp_path: Path) -> None:
    """088 (FR-009): the bootstrap circuit-breaker budget — ``bootstrap_attempts``
    and ``bootstrap_exhausted`` — persists across save → load so a restart does
    not hand a failing bootstrap a fresh unlimited budget."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot(
        env_cache={
            "sym-one": EnvCacheStateSnapshot(
                symphony_name="sym-one",
                sanitised_name="sym-one-a1b2c3",
                cache_dir="/devenv/sym-one",
                readme_sha="deadbeef",
                last_bootstrap_succeeded=False,
                bootstrap_attempts=2,
                bootstrap_exhausted=True,
            ),
        },
    )

    await store.save(snapshot)
    loaded = await store.load()

    assert loaded is not None
    entry = loaded.env_cache["sym-one"]
    assert entry.bootstrap_attempts == 2
    assert entry.bootstrap_exhausted is True


@pytest.mark.asyncio
async def test_old_env_cache_snapshot_defaults_bootstrap_budget(tmp_path: Path) -> None:
    """088 (FR-009): an old (pre-088) snapshot without the budget fields loads
    with safe defaults (0 attempts, not exhausted) — no migration step."""
    import json

    metrics = CoordinareMetrics()
    path = tmp_path / "state.json"
    store = StateStore(path=path, metrics=metrics)
    snapshot = _make_snapshot(
        env_cache={
            "sym-one": EnvCacheStateSnapshot(
                symphony_name="sym-one",
                sanitised_name="sym-one-a1b2c3",
                cache_dir="/devenv/sym-one",
            ),
        },
    )
    await store.save(snapshot)

    data = json.loads(path.read_text())
    data["env_cache"]["sym-one"].pop("bootstrap_attempts", None)
    data["env_cache"]["sym-one"].pop("bootstrap_exhausted", None)
    path.write_text(json.dumps(data))

    loaded = await store.load()
    assert loaded is not None
    entry = loaded.env_cache["sym-one"]
    assert entry.bootstrap_attempts == 0
    assert entry.bootstrap_exhausted is False


# --- 123 T004: split bounce budget + assessor Q&A carryover on PersistedSession ---


def test_persisted_session_split_budget_fields_default_to_zero_and_empty() -> None:
    """123: a freshly-constructed PersistedSession has the new fields at safe defaults."""
    sess = PersistedSession(card_id="PVTI_123")
    assert sess.content_feedback_cycles == 0
    assert sess.transient_error_cycles == 0
    assert sess.assessor_open_questions == []


def test_persisted_session_accepts_split_budget_values() -> None:
    """123: the new fields accept explicit values and preserve assessor Q&A dicts."""
    qa = [{"question": "Should auth use OAuth?", "answer": "Yes, OAuth2 PKCE"}]
    sess = PersistedSession(
        card_id="PVTI_123",
        content_feedback_cycles=2,
        transient_error_cycles=3,
        assessor_open_questions=qa,
    )
    assert sess.content_feedback_cycles == 2
    assert sess.transient_error_cycles == 3
    assert sess.assessor_open_questions == qa


def test_persisted_session_migrates_legacy_feedback_cycle_count() -> None:
    """123 (schema v12): a snapshot carrying legacy feedback_cycle_count seeds
    content_feedback_cycles when the new field is absent."""
    sess = PersistedSession.model_validate(
        {
            "card_id": "PVTI_123",
            "feedback_cycle_count": 3,
            # content_feedback_cycles absent
        }
    )
    assert sess.content_feedback_cycles == 3  # migrated from legacy field
    assert sess.transient_error_cycles == 0  # default
    assert sess.assessor_open_questions == []  # default


def test_persisted_session_legacy_migration_does_not_override_explicit_value() -> None:
    """123: an explicit non-zero content_feedback_cycles wins over the legacy field."""
    sess = PersistedSession.model_validate(
        {
            "card_id": "PVTI_123",
            "feedback_cycle_count": 3,
            "content_feedback_cycles": 5,
        }
    )
    assert sess.content_feedback_cycles == 5


def test_persisted_session_legacy_migration_ignores_zero_and_bool() -> None:
    """123: a zero / bool legacy value is not treated as a positive migration source."""
    zero = PersistedSession.model_validate(
        {"card_id": "PVTI_123", "feedback_cycle_count": 0}
    )
    assert zero.content_feedback_cycles == 0
    truthy = PersistedSession.model_validate(
        {"card_id": "PVTI_123", "feedback_cycle_count": True}
    )
    assert truthy.content_feedback_cycles == 0
