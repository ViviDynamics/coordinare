"""Compare SQLite writer strategies with identical rows, transactions and schema."""
from __future__ import annotations

import json
import sqlite3
import statistics
import sys
import time
from typing import TYPE_CHECKING

from coordinare.services.event_store_db import COLUMNS, create_schema

if TYPE_CHECKING:
    from collections.abc import Callable

BATCH_SIZE = 128
BATCHES = 100
REPEATS = 5
INSERT = 'INSERT OR IGNORE INTO events (' + ','.join(COLUMNS) + ') VALUES (' + ','.join('?' for _ in COLUMNS) + ')'
ROWS = [('2026-10-15T12:00:00Z', 'samplehost', '271', 'run', 'implementing', 'performer', 'progress', 'info', 'message', '{}')] * BATCH_SIZE


def batch(db: sqlite3.Connection) -> None:
    db.executemany(INSERT, ROWS)


def returning(db: sqlite3.Connection) -> None:
    for row in ROWS:
        db.execute(INSERT + ' RETURNING id', row).fetchone()


def batch_read(db: sqlite3.Connection) -> None:
    before = db.execute('SELECT coalesce(max(id),0) FROM events').fetchone()[0]
    inserted = db.executemany(INSERT, ROWS)
    if inserted.rowcount != len(ROWS):
        db.execute('SELECT ' + ','.join(COLUMNS) + ' FROM events WHERE id > ? ORDER BY id', (before,)).fetchall()


def measure(strategy: Callable[[sqlite3.Connection], None], *, fts: bool) -> float:
    timings = []
    for _ in range(REPEATS):
        with sqlite3.connect(':memory:') as db:
            create_schema(db, enable_fts=fts)
            started = time.perf_counter()
            for _ in range(BATCHES):
                with db:
                    strategy(db)
            timings.append(time.perf_counter() - started)
        db.close()
    return BATCH_SIZE * BATCHES / statistics.median(timings)


def main() -> None:
    for fts in (False, True):
        rates = {name: measure(strategy, fts=fts) for name, strategy in [('batch', batch), ('returning', returning), ('batch_read', batch_read)]}
        sys.stdout.write(json.dumps({'fts': fts, 'rows_per_second': rates, 'returning_throughput_loss': 1 - rates['returning'] / rates['batch'], 'batch_read_throughput_loss': 1 - rates['batch_read'] / rates['batch']}) + '\n')


if __name__ == '__main__':
    main()
