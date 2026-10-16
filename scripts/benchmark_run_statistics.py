"""Measure a cold 30 day statistics query over 2000 durable runs without sockets."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from coordinare.services.event_store_db import COLUMNS, create_schema
from coordinare.services.run_statistics import RunStatistics
from coordinare.services.run_transcript import transcript_path
from coordinare.services.stats_params import StatsQuery
from coordinare.services.token_usage import ModelPrice, PriceTable

RUN_COUNT = 2000
WINDOW_DAYS = 30
MESSAGES_PER_RUN = 20
PROGRESS_BYTES = 32 * 1024
INPUT_TOKENS = 100
OUTPUT_TOKENS = 50
RUN_SECONDS = 60


def fixture(root: Path, path: Path, now: datetime) -> int:
    rows = []
    size = 0
    start = now - timedelta(days=WINDOW_DAYS)
    for index in range(RUN_COUNT):
        run = f"run{index}"
        began = start + timedelta(seconds=(now - start).total_seconds() * index / RUN_COUNT)
        header = {"type": "run_header", "ts": began.isoformat(), "run_id": run,
                  "persona": {"name": "implementer"}, "performer": {"backend": "codex"}, "model": {"model": "m"}}
        records = [header, {"type": "assistant_text", "text": "x" * PROGRESS_BYTES}]
        for seq in range(1, MESSAGES_PER_RUN + 1):
            record = {"type": "usage", "seq": seq, "ts": began.isoformat(), "scope": "message",
                      "usage": {"model": "m", "input_tokens": INPUT_TOKENS, "output_tokens": OUTPUT_TOKENS}}
            records.append(record)
            rows.append((began.isoformat(), "alpha", run, run, "implementing", "daemon", "run_usage", "info", "", json.dumps(record)))
        records.append({"type": "result", "ts": (began + timedelta(seconds=RUN_SECONDS)).isoformat(), "is_error": False})
        transcript = transcript_path(root, symphony="alpha", card_id=run, run_id=run)
        transcript.parent.mkdir(parents=True, exist_ok=True)
        content = "".join(json.dumps(record) + "\n" for record in records)
        transcript.write_text(content)
        size += len(content)
    with sqlite3.connect(path) as db:
        create_schema(db, enable_fts=False)
        db.execute("INSERT INTO events(ts, kind) VALUES (?, 'started')", ((start - timedelta(seconds=RUN_SECONDS)).isoformat(),))
        db.executemany(
            "INSERT INTO events (" + ",".join(COLUMNS) + ") VALUES (" + ",".join("?" for _ in COLUMNS) + ")", rows,
        )
    return size


async def main() -> None:
    now = datetime.now(UTC)
    with tempfile.TemporaryDirectory(prefix="stats182-") as directory:
        folder = Path(directory)
        root, path = folder / "transcripts", folder / "events.db"
        size = fixture(root, path, now)
        service = RunStatistics(root, event_path=path, table=PriceTable(rules={"m": ModelPrice(1, 2, 3, 4)}))
        before = time.perf_counter()
        result = await service.query(StatsQuery(window="30d"), visible=frozenset({"alpha"}), lifecycle={}, boards={}, now=now)
        cold = time.perf_counter() - before
        before = time.perf_counter()
        await service.query(StatsQuery(window="30d", group_by="stage"), visible=frozenset({"alpha"}), lifecycle={}, boards={}, now=now)
        cached = time.perf_counter() - before
        sys.stdout.write(json.dumps({"runs": result["totals"]["runs"], "usage_events": RUN_COUNT * MESSAGES_PER_RUN,
                          "transcript_bytes": size, "cold_seconds": cold, "cached_seconds": cached,
                          "missing_usage_runs": result["totals"]["missing_usage_runs"],
                          "tokens": result["totals"]["tokens"]["total"], "usd": result["totals"]["cost"]["usd"],
                          "partial_reasons": result["partial_reasons"]}, indent=2) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
