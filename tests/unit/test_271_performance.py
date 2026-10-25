from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from time import perf_counter

import pytest

from coordinare.services.event_store import INSERT_BATCH_SIZE, EventStore
from coordinare.services.event_store_db import BASELINE_INDEXES, FILTER_INDEXES, create_schema

BENCHMARK_ROWS = 2000
PAYLOAD_REPEATS = 16
SECONDS_TO_MICROSECONDS = 1_000_000
WAIT_SECONDS = 30.0


@pytest.mark.asyncio
async def test_typical_event_write_throughput_and_loop_overhead(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.db")
    await store.start()
    payload = {
        "symphony": "samplehost",
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
    for seq in range(BENCHMARK_ROWS):
        row = {**payload, "data": {**payload["data"], "transcript_seq": seq}}
        assert store.emit(row)
    enqueued = perf_counter()
    await asyncio.wait_for(store.flush(), timeout=WAIT_SECONDS)
    await store.close()
    finished = perf_counter()
    assert store.health()["rows"] == BENCHMARK_ROWS
    assert store.health()["dropped"] == 0
    print(
        f"benchmark_rows={BENCHMARK_ROWS} rows_per_second={BENCHMARK_ROWS / (finished - started):.1f} enqueue_us={(enqueued - started) * SECONDS_TO_MICROSECONDS / BENCHMARK_ROWS:.1f}",
    )


INDEX_SEED_ROWS = 100_000
INDEX_WRITE_ROWS = 20_000
INDEX_REPEATS = 3
ORDER_REVERSAL_PERIOD = 2
PERCENT_SCALE = 100
INDEX_VARIANTS = (4, 7, 8)
BENCHMARK_FILTER_INDEXES = (*FILTER_INDEXES, ("stage_ts", "stage, ts"))
BENCHMARK_TIMEOUT_SECONDS = 90
SYMPHONY_COUNT = 20
CARD_COUNT = 1000
RUN_COUNT = 100
KINDS_COUNT = 8
SOURCES = ("daemon", "performer", "bootstrap", "operator")
STAGES = ("implementer", "reviewer", "tester", "closer")
LEVELS = ("info", "warning", "error")
INDEX_PAYLOAD_REPEATS = 4
INDEX_STAMP = datetime(2026, 10, 15, tzinfo=UTC)
INSERT_EVENT = "INSERT INTO events (ts,symphony,card_id,run_id,stage,source,kind,level,message,data) VALUES (?,?,?,?,?,?,?,?,?,?)"


def _index_row(seq: int) -> tuple[str, ...]:
    return (
        (INDEX_STAMP + timedelta(seconds=seq)).isoformat(),
        f"symphony{seq % SYMPHONY_COUNT}", f"card{seq % CARD_COUNT}",
        f"run{seq % RUN_COUNT}", STAGES[seq % len(STAGES)], SOURCES[seq % len(SOURCES)],
        f"kind{seq % KINDS_COUNT}", LEVELS[seq % len(LEVELS)],
        "Inspect event storage and capture " * INDEX_PAYLOAD_REPEATS,
        json.dumps({"transcript_seq": seq, "tool": "read_file", "path": "src/coordinare/services/event_store.py"}),
    )


@pytest.mark.timeout(BENCHMARK_TIMEOUT_SECONDS)
def test_271_filter_index_write_throughput(tmp_path: Path) -> None:
    """Compare only SQLite cost, retaining FTS and replay protection in both cases."""
    seed_path = tmp_path / "seed.db"
    with sqlite3.connect(seed_path) as db:
        create_schema(db)
        db.executemany(INSERT_EVENT, (_index_row(seq) for seq in range(INDEX_SEED_ROWS)))
    rows = [_index_row(INDEX_SEED_ROWS + seq) for seq in range(INDEX_WRITE_ROWS)]
    rates: dict[int, list[float]] = {variant: [] for variant in INDEX_VARIANTS}
    for repeat in range(INDEX_REPEATS):
        # Reverse the order on alternating rounds to reduce cache ordering bias.
        variants = INDEX_VARIANTS if repeat % ORDER_REVERSAL_PERIOD == 0 else tuple(reversed(INDEX_VARIANTS))
        for variant in variants:
            path = tmp_path / f"benchmark{repeat}_{variant}.db"
            shutil.copyfile(seed_path, path)
            with sqlite3.connect(path) as db:
                create_schema(db)
                for name, columns in BENCHMARK_FILTER_INDEXES[:variant - len(BASELINE_INDEXES)]:
                    with db:
                        db.execute(f"CREATE INDEX events_{name} ON events ({columns})")
                started = perf_counter()
                for offset in range(0, INDEX_WRITE_ROWS, INSERT_BATCH_SIZE):
                    with db:
                        db.executemany(INSERT_EVENT, rows[offset:offset + INSERT_BATCH_SIZE])
                elapsed = perf_counter() - started
                rates[variant].append(INDEX_WRITE_ROWS / elapsed)
                assert db.execute("SELECT count(*) FROM events").fetchone()[0] == INDEX_SEED_ROWS + INDEX_WRITE_ROWS
    for variant, samples in rates.items():
        print(f"filter_indexes={variant} sqlite_rows_per_second_median={median(samples):.1f} samples={samples}")
    baseline = median(rates[INDEX_VARIANTS[0]])
    for variant in INDEX_VARIANTS[1:]:
        candidate = median(rates[variant])
        print(f"filter_indexes={variant} throughput_drop_percent={(1 - candidate / baseline) * PERCENT_SCALE:.2f}")
