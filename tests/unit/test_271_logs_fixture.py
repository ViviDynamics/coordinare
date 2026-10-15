"""Check browser seed data without starting its HTTP server."""
from __future__ import annotations

from itertools import count
from pathlib import Path

import pytest

from coordinare.services.event_store import EventStore
from tests.e2e.test_271_logs_page import ALPHA, BETA, SEED_ROWS, _emit


@pytest.mark.asyncio
async def test_browser_fixture_keeps_every_performer_row(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    sequence = count()
    await store.start()
    try:
        for index in range(SEED_ROWS):
            await _emit(store, f"alpha row {index}", ALPHA, sequence)
        await _emit(store, "beta marker", BETA, sequence)
        rows = (await store.query_events())["events"]
        assert len(rows) == SEED_ROWS + 1
        assert {row["data"]["transcript_seq"] for row in rows} == set(range(SEED_ROWS + 1))
        assert store.health()["dropped"] == 0
    finally:
        await store.close()
