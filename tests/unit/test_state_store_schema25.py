"""354: schema v25 — ``slot_queued_since`` on PersistedSession.

The queued-since marker records the first cycle a card wanted a performer
slot and none was free. It must survive a daemon restart (it is the
operator's answer to "how long has this card been waiting?"), so it lives
on PersistedSession with a backward-compatible None default.
"""
from __future__ import annotations

from datetime import UTC, datetime

from coordinare.state_store import CURRENT_SCHEMA_VERSION, PersistedSession, WorkflowSnapshot


class TestSlotQueuedSinceSchemaV25:
    def test_schema_version_is_25(self) -> None:
        """545 advances the schema to 26; queued-since remains supported."""
        assert CURRENT_SCHEMA_VERSION == 30

    def test_slot_queued_since_defaults_none(self) -> None:
        session = PersistedSession(card_id="test-card")
        assert session.slot_queued_since is None

    def test_slot_queued_since_round_trips(self) -> None:
        """The marker survives snapshot save/load (daemon restart)."""
        queued_at = datetime(2026, 9, 23, 12, 0, 0, tzinfo=UTC)
        snapshot = WorkflowSnapshot(
            snapshot_at=datetime.now(UTC),
            phase="dispatching",
            active_sessions={
                "PVTI_Q": PersistedSession(card_id="PVTI_Q", slot_queued_since=queued_at),
            },
        )
        raw = snapshot.model_dump_json()
        restored = WorkflowSnapshot.model_validate_json(raw)
        assert restored.active_sessions["PVTI_Q"].slot_queued_since == queued_at

    def test_v24_snapshot_without_marker_still_loads(self) -> None:
        """A v24 snapshot (pre-354) loads unchanged; marker defaults None."""
        raw = WorkflowSnapshot(
            schema_version=24,
            snapshot_at=datetime.now(UTC),
            phase="dispatching",
            active_sessions={
                "PVTI_OLD": PersistedSession(card_id="PVTI_OLD"),
            },
        ).model_dump_json()
        # The v24 payload has no schema_version bump of its own — stamp it.
        import json

        payload = json.loads(raw)
        payload["schema_version"] = 24
        # A true v24 payload has no slot_queued_since key at all — the
        # current model dumps it as null, which would defeat the
        # backward-compatibility check.
        del payload["active_sessions"]["PVTI_OLD"]["slot_queued_since"]
        raw = json.dumps(payload)
        snapshot = WorkflowSnapshot.model_validate_json(raw)
        assert snapshot.schema_version == 24
        assert snapshot.active_sessions["PVTI_OLD"].slot_queued_since is None
