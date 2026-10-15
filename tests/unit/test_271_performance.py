from __future__ import annotations

from pathlib import Path
from time import perf_counter

import pytest

from coordinare.services.event_store import EventStore

BENCHMARK_ROWS = 2000
PAYLOAD_REPEATS = 16
SECONDS_TO_MICROSECONDS = 1_000_000


@pytest.mark.asyncio
async def test_typical_event_write_throughput_and_loop_overhead(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    await store.start()
    payload = {
        "symphony": "garden",
        "card_id": "271",
        "run_id": "benchmark",
        "stage": "implementer",
        "source": "performer",
        "kind": "tool_use",
        "message": "Inspect event storage and capture",
        "data": {
            "tool": "read_file",
            "arg": "src/coordinare/services/event_store.py",
            "transcript_seq": 1,
            "detail": "This representative payload contains ordinary tool arguments. "
            * PAYLOAD_REPEATS,
        },
    }
    started = perf_counter()
    for _ in range(BENCHMARK_ROWS):
        assert store.emit(payload)
    enqueued = perf_counter()
    await store.close()
    finished = perf_counter()
    assert store.health()["rows"] == BENCHMARK_ROWS
    assert store.health()["dropped"] == 0
    print(
        f"benchmark_rows={BENCHMARK_ROWS} rows_per_second={BENCHMARK_ROWS / (finished - started):.1f} enqueue_us={(enqueued - started) * SECONDS_TO_MICROSECONDS / BENCHMARK_ROWS:.1f}",
    )
