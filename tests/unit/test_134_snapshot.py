"""#134: the dashboard snapshot carries the usage ledger's figures, or says it is not tracked.

Built through the real ``DashboardStore.build_snapshot`` with a mock daemon whose
state holds a real transcript collector and a real cost_tracking config.
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from coordinare.config import CostTrackingConfig
from coordinare.dashboard import DashboardStore
from coordinare.services.run_transcript import TranscriptCollector

NOW_ISO = datetime.now(UTC).isoformat()


def _daemon(collector: TranscriptCollector | None, *, cost: dict[str, Any] | None = None,
            dispatch: dict[str, Any] | None = None, card: str = "PVTI_134") -> MagicMock:
    daemon = MagicMock()
    config = MagicMock()
    config.cost_tracking = CostTrackingConfig.model_validate(cost or {})
    state: dict[str, Any] = {
        "phase": "monitoring_performer",
        "error_count": 0,
        "config": config,
        "transcript_collector": collector,
        "current_card": {"id": card, "title": "Add metering"},
        "performer_stage": "implementing",
        "agent_dispatch": dispatch if dispatch is not None else {"session_id": "run-134"},
        "agent_dispatch_at": datetime.now(UTC),
        "card_tokens_total": 19350,
    }
    daemon.state = state
    daemon.state_store = MagicMock()
    daemon.state_store.last_snapshot = None
    daemon._cycle_active = False
    daemon.running = True
    return daemon


def _metrics() -> MagicMock:
    metrics = MagicMock()
    metrics.cycles_completed_total._value.get.return_value = 0
    metrics.build_info.labels.return_value._value.get.return_value = {"started_at": "2026-10-13T09:30:00+00:00"}
    return metrics


def _health() -> MagicMock:
    health = MagicMock()
    probe = MagicMock()
    probe.subsystem_name = "github"
    probe.status.value = "healthy"
    probe.is_required = True
    probe.checked_at.isoformat.return_value = NOW_ISO
    probe.details = None
    health.snapshot.return_value.probes = [probe]
    return health


def _priced_collector(root: Path) -> TranscriptCollector:
    collector = TranscriptCollector(root, enabled=True, max_bytes_per_run=10**6)
    collector.usage.observe(
        "run-134", symphony="acme", card="PVTI_134", stage="implementing",
        records=[{
            "ts": NOW_ISO, "seq": 1, "type": "usage", "scope": "message",
            "usage": {"input_tokens": 1_000_000, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "model": "m-1"},
        }],
    )
    return collector


_PRICES = {
    "pricing_as_of": "2026-10-01",
    "prices": [{
        "models": ["m-1"], "input_per_million": 3.0, "output_per_million": 0.0,
        "cache_read_per_million": 0.0, "cache_write_per_million": 0.0,
    }],
}


def _snapshot(daemon: MagicMock) -> dict[str, Any]:
    return DashboardStore().build_snapshot(daemon, _metrics(), _health())


def test_the_snapshot_prices_today_and_the_week_from_the_ledger(tmp_path: Path) -> None:
    snapshot = _snapshot(_daemon(_priced_collector(tmp_path / "t"), cost=_PRICES))
    block = snapshot["token_usage"]
    assert block["tracked"] is True
    assert block["today"]["tokens"]["input"] == 1_000_000
    assert block["today"]["cost"]["usd"] == 3.0
    assert block["today"]["pricing"] == {"as_of": "2026-10-01", "source": "table"}
    assert block["week"]["cost"]["usd"] == 3.0


def test_the_active_session_carries_its_run_and_card_usage(tmp_path: Path) -> None:
    snapshot = _snapshot(_daemon(_priced_collector(tmp_path / "t"), cost=_PRICES))
    sessions = snapshot["active_sessions"]
    assert sessions, "the card's live session is in the snapshot"
    usage = sessions[0]["usage"]
    assert usage["tracked"] is True
    assert usage["run"]["cost"]["usd"] == 3.0
    assert usage["card"]["tokens"]["total"] == 1_000_000


def test_transcripts_off_says_not_tracked_and_has_no_figures(tmp_path: Path) -> None:
    off = TranscriptCollector(tmp_path / "off", enabled=False, max_bytes_per_run=10**6)
    snapshot = _snapshot(_daemon(off, cost=_PRICES))
    assert snapshot["token_usage"] == {
        "tracked": False, "reason": "transcripts disabled", "day": None,
        "week_start": None, "today": None, "week": None,
    }
    usage = snapshot["active_sessions"][0]["usage"]
    assert usage["tracked"] is False
    assert usage["reason"] == "transcripts disabled"


def test_no_collector_at_all_is_not_tracked(tmp_path: Path) -> None:
    snapshot = _snapshot(_daemon(None))
    assert snapshot["token_usage"]["tracked"] is False


def test_an_unpriced_model_is_unknown_in_the_snapshot_not_zero(tmp_path: Path) -> None:
    snapshot = _snapshot(_daemon(_priced_collector(tmp_path / "t"), cost={}))
    today = snapshot["token_usage"]["today"]
    assert today["tokens"]["input"] == 1_000_000
    assert today["cost"]["usd"] is None
    assert today["pricing"]["source"] == "none"
    assert "m-1" in today["cost"]["unknown_reason"]


def test_the_flat_rate_key_has_left_the_snapshot(tmp_path: Path) -> None:
    snapshot = _snapshot(_daemon(_priced_collector(tmp_path / "t"), cost=_PRICES))
    assert "cost_per_million_tokens" not in snapshot
