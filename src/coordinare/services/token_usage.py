"""Token and cost accounting from transcript ``usage`` records (spec 134).

The source of truth is the ``usage`` records the daemon accepts onto the run
transcript (spec 129). The writer hands every accepted record to :class:`TokenLedger`
(``observe``), so totals rise on the poll that absorbs a record. After a restart the
ledger is rebuilt from the transcript files inside the window
(``load_from_transcripts``), and the writer continues from the cursor the file holds,
so nothing is counted twice.

Per run, one tier of records is the total (see :meth:`_Run.buckets`):

1. ``message`` records, when any exist. These are per model message and carry the model.
2. otherwise the run records. They are cumulative, so each UTC day's share is the step
   from the previous day's last cumulative value to that day's last value.
3. otherwise ``scope``-less records, summed, and the run is flagged ``unscoped``.

Cost comes from a per-model price table (:class:`PriceTable`), built from the
``cost_tracking`` config. A model with no price keeps its token counts and its cost
is reported unknown, with the reason. No price is ever guessed. Money is exact
``Decimal`` internally and becomes a float rounded to micro-dollars at the snapshot.

Day totals are keyed by the UTC day of each record's timestamp.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.lib.path_segments import (
    CONDUCTOR_FOLDER,
    UNATTRIBUTED,
    is_conductor_folder,
    sanitize_segment,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path
    from typing import BinaryIO

    OpenFile = Callable[[Path], BinaryIO | None]

    from coordinare.config import PriceRates

logger = structlog.get_logger(__name__)

# The ledger keeps the 7-day week plus one day of slack. Older runs are pruned.
USAGE_WINDOW_DAYS = 8
WEEK_DAYS = 7
# Answers kept by TokenLedger.totals between writes (150: the cache is bounded).
TOTALS_CACHE_MAX = 256
# Per-turn entries kept for one run, and across all runs. Totals stay exact beyond these.
MAX_TURNS_PER_RUN = 2000
MAX_TURNS_TOTAL = 20_000
# Bounds on the boot rebuild: total bytes, wall-clock seconds, and one usage line.
REBUILD_MAX_BYTES = 256 * 1024 * 1024
REBUILD_MAX_SECONDS = 30.0
USAGE_LINE_MAX_BYTES = 1024 * 1024

_SCOPES: frozenset[str] = frozenset({"message", "run"})
_USAGE_TYPE = "usage"
_USAGE_MARKER = b'"usage"'
_TRANSCRIPT_SUFFIX = ".jsonl"
_NO_PRICE = "no price configured"
_NO_MODEL = "model not recorded in the usage record"
_MILLION = Decimal(1_000_000)
MICRO_DOLLAR = Decimal("0.000001")
FLAT_SOURCE = "deprecated_flat_rate"
TABLE_SOURCE = "table"
NONE_SOURCE = "none"


def _count(value: object) -> int:
    """A real, non-negative token count. Booleans, floats and junk count as zero."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return 0


@dataclass(frozen=True)
class UsageCounts:
    """Tokens of the four kinds. Cache reads and writes are kept apart from input."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read + self.cache_write

    def __add__(self, other: UsageCounts) -> UsageCounts:
        return UsageCounts(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read=self.cache_read + other.cache_read,
            cache_write=self.cache_write + other.cache_write,
        )

    def minus(self, other: UsageCounts) -> UsageCounts:
        """The step from ``other`` to ``self``, never below zero for any kind."""
        return UsageCounts(
            input_tokens=max(0, self.input_tokens - other.input_tokens),
            output_tokens=max(0, self.output_tokens - other.output_tokens),
            cache_read=max(0, self.cache_read - other.cache_read),
            cache_write=max(0, self.cache_write - other.cache_write),
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> UsageCounts:
        return cls(
            input_tokens=_count(raw.get("input_tokens")),
            output_tokens=_count(raw.get("output_tokens")),
            cache_read=_count(raw.get("cache_read")),
            cache_write=_count(raw.get("cache_write")),
        )


ModelTotals = dict[str | None, UsageCounts]


def day_of(ts: object) -> str | None:
    """The UTC calendar day (ISO) of a record timestamp, or None when it does not parse."""
    if not isinstance(ts, str) or not ts:
        return None
    try:
        moment = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).date().isoformat()


@dataclass(frozen=True)
class UsageTurn:
    """One model turn: the record's seq, UTC day, model, scope and four counts."""

    seq: int
    day: str
    model: str | None
    scope: str | None
    counts: UsageCounts


@dataclass(frozen=True)
class RunInfo:
    """What the ledger knows about one run. ``unscoped`` and ``turns_dropped`` say when totals are partial."""

    run_id: str
    symphony: str
    card: str
    stage: str
    last_seq: int
    unscoped: bool
    turns_dropped: bool


@dataclass
class _Run:
    run_id: str
    ingested_day: str
    symphony: str = ""
    card: str = ""
    stage: str = ""
    last_seq: int = 0
    last_day: str = ""
    turns: list[UsageTurn] = field(default_factory=list)
    turns_dropped: bool = False
    message: dict[tuple[str, str | None], UsageCounts] = field(default_factory=dict)
    unscoped_buckets: dict[tuple[str, str | None], UsageCounts] = field(default_factory=dict)
    # UTC day -> (model, cumulative total as of that day's last run record).
    run_by_day: dict[str, tuple[str | None, UsageCounts]] = field(default_factory=dict)

    def buckets(self) -> dict[tuple[str, str | None], UsageCounts]:
        """The tier rule: message, else the cumulative run steps, else unscoped."""
        if self.message:
            return self.message
        if self.run_by_day:
            return _cumulative_steps(self.run_by_day)
        return self.unscoped_buckets

    def info(self) -> RunInfo:
        return RunInfo(
            run_id=self.run_id,
            symphony=self.symphony,
            card=self.card,
            stage=self.stage,
            last_seq=self.last_seq,
            unscoped=not self.message and not self.run_by_day and bool(self.unscoped_buckets),
            turns_dropped=self.turns_dropped,
        )


def _cumulative_steps(by_day: Mapping[str, tuple[str | None, UsageCounts]]) -> dict[tuple[str, str | None], UsageCounts]:
    """Each day's share of a cumulative total: its last value minus the previous day's last value.

    A run that spans midnight is split between the days it ran on. The steps sum to the
    final cumulative value, so the run total is unchanged.
    """
    steps: dict[tuple[str, str | None], UsageCounts] = {}
    previous = UsageCounts()
    for day in sorted(by_day):
        model, cumulative = by_day[day]
        step = cumulative.minus(previous)
        if step.total:
            steps[(day, model)] = step
        previous = cumulative
    return steps


@dataclass
class RebuildReport:
    """What one boot rebuild read and what it had to skip, for the log and for tests."""

    files_read: int = 0
    files_skipped: int = 0
    dirs_skipped: int = 0
    bytes_read: int = 0
    usage_records: int = 0
    oversize_usage_lines: int = 0
    stopped_by: str = ""


class TokenLedger:
    """Per-run usage buckets keyed by (UTC day, model), with rollups and a disk rebuild.

    ``observe`` is cheap and runs on the event loop. Every public method takes one lock,
    so the boot-time rebuild on another thread is safe. Read results are cached until
    the next change (``version``).
    """

    def __init__(self, *, window_days: int = USAGE_WINDOW_DAYS) -> None:
        self._window_days = window_days
        self._runs: dict[str, _Run] = {}
        self._version = 0
        self._cache: dict[tuple[object, ...], ModelTotals] = {}
        self._turn_total = 0
        self._lock = threading.Lock()
        self.last_rebuild = RebuildReport()

    @property
    def version(self) -> int:
        """Moves whenever a record changes a total. Callers may cache by it."""
        return self._version

    def observe(
        self,
        run_id: str,
        *,
        symphony: str,
        card: str,
        stage: str,
        records: Iterable[Mapping[str, Any]],
    ) -> int:
        """Count the usage records of one run. Returns how many were counted.

        A record at or below the run's last seq is skipped, so a redelivered record is
        never counted twice. A record's own ``stage`` key wins over the argument.
        """
        # 139 review: the ledger keys a run by the folder its transcript was written to,
        # so the symphony is the sanitised name the writer used. 131: the one exception is the
        # conductor's own folder, which no sanitised name can equal. Every caller passes either
        # a sanitised folder or a raw symphony name, so this is the only admitted bypass.
        symphony = symphony if is_conductor_folder(symphony) else sanitize_segment(symphony)
        counted = 0
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                run = _Run(run_id=run_id, ingested_day=datetime.now(UTC).date().isoformat())
                self._runs[run_id] = run
            run.symphony = run.symphony or symphony
            run.card = run.card or card
            for raw in records:
                if self._take(run, raw, fallback_stage=stage):
                    counted += 1
            if counted:
                self._version += 1
                self._cache.clear()
        return counted

    def _take(self, run: _Run, raw: Mapping[str, Any], *, fallback_stage: str) -> bool:
        seq = raw.get("seq")
        if raw.get("type") != _USAGE_TYPE or not isinstance(seq, int) or isinstance(seq, bool):
            return False
        if seq <= run.last_seq:
            return False
        run.last_seq = seq
        usage = raw.get("usage")
        if not isinstance(usage, dict):
            return False
        counts = UsageCounts.from_mapping(usage)
        if counts.total == 0:
            return False
        model_raw = usage.get("model")
        model = model_raw if isinstance(model_raw, str) and model_raw else None
        day = day_of(raw.get("ts")) or ""
        scope = raw.get("scope") if raw.get("scope") in _SCOPES else None
        stage_raw = raw.get("stage")
        stage = stage_raw if isinstance(stage_raw, str) and stage_raw else fallback_stage
        run.stage = run.stage or stage
        if day and day > run.last_day:
            run.last_day = day
        if scope == "run":
            # Cumulative: the last run record of a day is that day's cumulative value.
            run.run_by_day[day] = (model, counts)
            return True
        key = (day, model)
        if scope == "message":
            run.message[key] = run.message.get(key, UsageCounts()) + counts
        else:
            run.unscoped_buckets[key] = run.unscoped_buckets.get(key, UsageCounts()) + counts
        self._keep_turn(run, UsageTurn(seq=seq, day=day, model=model, scope=scope, counts=counts))
        return True

    def _keep_turn(self, run: _Run, turn: UsageTurn) -> None:
        """Keep one turn's detail within the per-run and the global budget (oldest runs give way)."""
        if len(run.turns) >= MAX_TURNS_PER_RUN:
            run.turns_dropped = True
            return
        if self._turn_total >= MAX_TURNS_TOTAL and not self._free_turns(keep=run):
            run.turns_dropped = True
            return
        run.turns.append(turn)
        self._turn_total += 1

    def _free_turns(self, *, keep: _Run) -> bool:
        """Drop the detail of the oldest runs (insertion order) until there is room. Totals stay."""
        for other in self._runs.values():
            if other is keep or not other.turns:
                continue
            self._turn_total -= len(other.turns)
            other.turns = []
            other.turns_dropped = True
            if self._turn_total < MAX_TURNS_TOTAL:
                return True
        return self._turn_total < MAX_TURNS_TOTAL

    def totals(
        self,
        *,
        run_id: str | None = None,
        card: str | None = None,
        stage: str | None = None,
        symphony: str | None = None,
        days: tuple[str, str] | None = None,
    ) -> ModelTotals:
        """Totals per model for every run matching the filters (AND-ed). No match is ``{}``.

        ``days`` is an inclusive (first, last) pair of ISO UTC days.
        """
        key: tuple[object, ...] = ("totals", run_id, card, stage, symphony, days)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return dict(cached)
            out: ModelTotals = {}
            for run in self._runs.values():
                if run_id is not None and run.run_id != run_id:
                    continue
                if card is not None and run.card != card:
                    continue
                if stage is not None and run.stage != stage:
                    continue
                if symphony is not None and run.symphony != symphony:
                    continue
                for (day, model), counts in run.buckets().items():
                    if days is not None and not (days[0] <= day <= days[1]):
                        continue
                    out[model] = out.get(model, UsageCounts()) + counts
            self._cache[key] = out
            # 150: the cache holds at most TOTALS_CACHE_MAX answers between writes, oldest first out.
            while len(self._cache) > TOTALS_CACHE_MAX:
                del self._cache[next(iter(self._cache))]
            return dict(out)

    def totals_by_run(self, run_ids: Iterable[str]) -> dict[str, ModelTotals]:
        """Each named run's totals, in one pass over the runs (150). Runs with no usage are absent.

        Not cached: a page asks once for all its runs, and the caller keeps the answer.
        """
        wanted = set(run_ids)
        found: dict[str, ModelTotals] = {}
        with self._lock:
            for run in self._runs.values():
                if run.run_id not in wanted:
                    continue
                per_model: ModelTotals = {}
                for (_day, model), counts in run.buckets().items():
                    per_model[model] = per_model.get(model, UsageCounts()) + counts
                found[run.run_id] = per_model
        return found

    def run_ids(self, *, card: str | None = None) -> list[str]:
        with self._lock:
            return [r.run_id for r in self._runs.values() if card is None or r.card == card]

    def run_info(self, run_id: str) -> RunInfo | None:
        with self._lock:
            run = self._runs.get(run_id)
            return run.info() if run is not None else None

    def turns(self, run_id: str) -> list[UsageTurn]:
        with self._lock:
            run = self._runs.get(run_id)
            return list(run.turns) if run is not None else []

    def prune(self, today: date) -> int:
        """Forget runs whose newest activity is older than the window. Returns how many.

        A run with no parseable timestamp is judged by the UTC day it was first seen.
        """
        cutoff = (today - timedelta(days=self._window_days - 1)).isoformat()
        with self._lock:
            stale = [rid for rid, run in self._runs.items() if (run.last_day or run.ingested_day) < cutoff]
            for rid in stale:
                self._turn_total -= len(self._runs[rid].turns)
                del self._runs[rid]
            if stale:
                self._version += 1
                self._cache.clear()
            return len(stale)

    def load_from_transcripts(
        self,
        root: Path,
        *,
        now: datetime,
        open_file: OpenFile | None = None,
    ) -> int:
        """Rebuild from ``<root>/<symphony>/<card>/<run>.jsonl`` files modified inside the window.

        Best effort: a directory or file that cannot be read is skipped and counted, and
        the rebuild never raises. Only ``usage`` lines are kept, read a line at a time with
        the line length, the total bytes and the wall-clock time all bounded. Returns how
        many usage records were counted. The outcome is in ``last_rebuild``.

        ``open_file`` opens each transcript (220). The daemon passes the mounted-file reader,
        which refuses a link anywhere on the path below a performer mount. The default refuses
        a link at the leaf and is for callers with no mounts to guard.
        """
        report = self._rebuild(root, now=now, open_file=open_file or _open_no_follow)
        self.last_rebuild = report
        logger.info(
            "token_usage.rebuilt",
            root=str(root),
            files_read=report.files_read,
            files_skipped=report.files_skipped,
            dirs_skipped=report.dirs_skipped,
            bytes_read=report.bytes_read,
            usage_records=report.usage_records,
            oversize_usage_lines=report.oversize_usage_lines,
            stopped_by=report.stopped_by or None,
            runs=len(self._runs),
        )
        return report.usage_records

    def _rebuild(self, root: Path, *, now: datetime, open_file: OpenFile) -> RebuildReport:
        report = RebuildReport()
        started = time.monotonic()
        window_start = (now - timedelta(days=self._window_days)).timestamp()
        files, dirs_skipped = _run_files(root)
        report.dirs_skipped = dirs_skipped
        for path in files:
            if time.monotonic() - started > REBUILD_MAX_SECONDS:
                report.stopped_by = "time"
                break
            try:
                info = path.stat()
            except OSError:
                report.files_skipped += 1
                continue
            if info.st_mtime < window_start:
                continue
            if report.bytes_read + info.st_size > REBUILD_MAX_BYTES:
                report.stopped_by = "bytes"
                break
            scanned = _scan_usage(path, open_file)
            if scanned is None:
                report.files_skipped += 1
                continue
            records, nbytes, oversize = scanned
            report.files_read += 1
            report.bytes_read += nbytes
            report.oversize_usage_lines += oversize
            report.usage_records += self.observe(
                path.stem,
                symphony=_file_symphony(path, open_file),
                card=path.parent.name,
                stage="",
                records=records,
            )
        self.prune(now.date())
        return report


def _open_no_follow(path: Path) -> BinaryIO | None:
    """The default transcript opener: a read that refuses a link at the leaf (220).

    Used only when a caller passes no opener. The daemon passes the mounted-file reader.
    """
    try:
        return os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb")
    except OSError:
        return None


def _file_symphony(path: Path, open_file: OpenFile) -> str:
    """The symphony a transcript file is attributed to (131).

    A symphony's runs are attributed to their folder. A conductor call is attributed to the
    symphony its header names, so its spend lands in that symphony's figures after a restart
    exactly as it did live. A conductor call whose header names none stays in the conductor's
    own folder.
    """
    folder = path.parent.parent.name
    if not is_conductor_folder(folder):
        return folder
    return _header_symphony(path, open_file) or folder


def _header_symphony(path: Path, open_file: OpenFile) -> str:
    """The ``conductor.symphony`` of a file's header line, or '' when it has none. Bounded and never raises."""
    try:
        handle = open_file(path)
        if handle is None:
            return ""
        with handle:
            raw = handle.readline(USAGE_LINE_MAX_BYTES)
        header = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return ""
    conductor = header.get("conductor") if isinstance(header, dict) else None
    name = conductor.get("symphony") if isinstance(conductor, dict) else None
    return name if isinstance(name, str) else ""


def _run_files(root: Path) -> tuple[list[Path], int]:
    """Run files two folders down (symphony, then card), in name order, and how many folders were skipped.

    Links are never followed. A folder that cannot be listed is skipped and counted, so one
    unreadable card cannot hide the rest.
    """
    found: list[Path] = []
    skipped = 0
    try:
        symphonies = sorted(root.iterdir())
    except OSError:
        return [], 1
    for symphony in symphonies:
        try:
            if symphony.is_symlink() or not symphony.is_dir():
                continue
            cards = sorted(symphony.iterdir())
        except OSError:
            skipped += 1
            continue
        for card in cards:
            try:
                if card.is_symlink() or not card.is_dir():
                    continue
                entries = sorted(card.iterdir())
            except OSError:
                skipped += 1
                continue
            found.extend(
                entry for entry in entries
                if entry.suffix == _TRANSCRIPT_SUFFIX and not entry.is_symlink() and entry.is_file()
            )
    return found, skipped


def _scan_usage(path: Path, open_file: OpenFile) -> tuple[list[dict[str, Any]], int, int] | None:
    """The usage records of one transcript file, read a bounded line at a time.

    Returns (records, bytes read, usage-looking lines skipped as oversize), or None when
    the file cannot be read or is refused by *open_file*. Only lines that name ``usage`` are
    parsed, and a line over ``USAGE_LINE_MAX_BYTES`` is skipped rather than held in memory.
    """
    records: list[dict[str, Any]] = []
    nbytes = 0
    oversize = 0
    try:
        handle = open_file(path)
        if handle is None:
            return None
        with handle:
            while True:
                line = handle.readline(USAGE_LINE_MAX_BYTES + 1)
                if not line:
                    break
                nbytes += len(line)
                if len(line) > USAGE_LINE_MAX_BYTES:
                    if _USAGE_MARKER in line:
                        oversize += 1
                    if not line.endswith(b"\n"):
                        nbytes += _discard_rest_of_line(handle)
                    continue
                if _USAGE_MARKER not in line:
                    continue
                try:
                    parsed = json.loads(line)
                except ValueError:
                    continue
                if isinstance(parsed, dict) and parsed.get("type") == _USAGE_TYPE:
                    records.append(parsed)
    except OSError:
        return None
    return records, nbytes, oversize


def _discard_rest_of_line(handle: Any) -> int:
    """Skip to the end of an oversize line without holding it. Returns the bytes skipped."""
    skipped = 0
    while True:
        chunk = handle.readline(USAGE_LINE_MAX_BYTES + 1)
        skipped += len(chunk)
        if not chunk or chunk.endswith(b"\n"):
            return skipped


# --- price table ---------------------------------------------------------------------


@dataclass(frozen=True)
class ModelPrice:
    """Dollars per million tokens, for each kind."""

    input_per_million: float
    output_per_million: float
    cache_read_per_million: float
    cache_write_per_million: float

    def kind_costs(self, counts: UsageCounts) -> dict[str, Decimal]:
        """Exact cost of each kind. Rates are read through their decimal text, so 0.1 is 0.1."""
        return {
            "input": _cost(counts.input_tokens, self.input_per_million),
            "output": _cost(counts.output_tokens, self.output_per_million),
            "cache_read": _cost(counts.cache_read, self.cache_read_per_million),
            "cache_write": _cost(counts.cache_write, self.cache_write_per_million),
        }


def _cost(tokens: int, rate_per_million: float) -> Decimal:
    return Decimal(tokens) * Decimal(str(rate_per_million)) / _MILLION


class PriceTable:
    """Resolves a model id to its price: exact id, then longest prefix, then the default.

    A flat rate (the deprecated ``cost_per_million_tokens``) prices every model, including
    an unrecorded one, and has no pricing date. Nothing else is ever guessed.
    """

    def __init__(
        self,
        *,
        rules: Mapping[str, ModelPrice] | None = None,
        default: ModelPrice | None = None,
        flat: ModelPrice | None = None,
        as_of: date | None = None,
        source: str = NONE_SOURCE,
    ) -> None:
        """``rules`` maps exact model ids and ``prefix*`` patterns to their price."""
        rules = rules or {}
        self._exact = {key: price for key, price in rules.items() if not key.endswith("*")}
        self._prefixes = {key[:-1]: price for key, price in rules.items() if key.endswith("*")}
        self._default = default
        self._flat = flat
        self.as_of = as_of
        self.source = source

    def resolve(self, model: str | None) -> ModelPrice | None:
        if model is None:
            return self._flat
        if model in self._exact:
            return self._exact[model]
        best: str | None = None
        for prefix in self._prefixes:
            if model.startswith(prefix) and (best is None or len(prefix) > len(best)):
                best = prefix
        if best is not None:
            return self._prefixes[best]
        if self._default is not None:
            return self._default
        return self._flat

    def basis(self, model: str | None) -> str:
        """Why a model is or is not priced, in words for the tooltip."""
        if model is None:
            return _NO_MODEL
        if self.resolve(model) is None:
            return _NO_PRICE
        if self.source == FLAT_SOURCE:
            return "deprecated flat rate, no pricing date"
        if model in self._exact:
            return "exact price"
        if any(model.startswith(prefix) for prefix in self._prefixes):
            return "prefix price"
        return "default price"


def price_table_from_config(cost_tracking: object) -> PriceTable:
    """Build the table from a ``cost_tracking`` config block (or None: no pricing).

    Duck-typed on purpose: the config model validates the shape, and the dashboard and
    the monitor may hold a partial or mocked config.
    """
    entries = getattr(cost_tracking, "prices", None) or []
    default_raw = getattr(cost_tracking, "default_price", None)
    flat_rate = getattr(cost_tracking, "cost_per_million_tokens", None)
    as_of = getattr(cost_tracking, "pricing_as_of", None)
    if entries or default_raw is not None:
        rules: dict[str, ModelPrice] = {}
        for entry in entries:
            price = _price_of(entry)
            for pattern in entry.models:
                rules[pattern] = price
        default = _price_of(default_raw) if default_raw is not None else None
        return PriceTable(
            rules=rules,
            default=default,
            as_of=as_of if isinstance(as_of, date) else None,
            source=TABLE_SOURCE,
        )
    if isinstance(flat_rate, (int, float)) and not isinstance(flat_rate, bool) and flat_rate >= 0:
        rate = float(flat_rate)
        flat = ModelPrice(rate, rate, rate, rate)
        return PriceTable(flat=flat, source=FLAT_SOURCE)
    return PriceTable(source=NONE_SOURCE)


def _price_of(entry: PriceRates) -> ModelPrice:
    return ModelPrice(
        input_per_million=entry.input_per_million,
        output_per_million=entry.output_per_million,
        cache_read_per_million=entry.cache_read_per_million,
        cache_write_per_million=entry.cache_write_per_million,
    )


# --- arithmetic and the snapshot view ------------------------------------------------


@dataclass(frozen=True)
class ModelLine:
    model: str | None
    tokens: UsageCounts
    price: ModelPrice | None
    basis: str
    cost_usd: Decimal | None
    kind_usd: dict[str, Decimal] | None


@dataclass(frozen=True)
class PricedTotals:
    tokens: UsageCounts
    models: tuple[ModelLine, ...]
    cost_usd: Decimal | None
    partial_usd: Decimal
    kind_usd: dict[str, Decimal]
    unknown_reason: str | None


def price_totals(totals: Mapping[str | None, UsageCounts], table: PriceTable) -> PricedTotals:
    """Price one set of model totals exactly. ``cost_usd`` is None when any token is unpriced."""
    tokens = UsageCounts()
    lines: list[ModelLine] = []
    kinds: dict[str, Decimal] = {"input": Decimal(0), "output": Decimal(0), "cache_read": Decimal(0), "cache_write": Decimal(0)}
    partial = Decimal(0)
    unknown: list[str] = []
    for model in sorted(totals, key=lambda m: (m is None, m or "")):
        counts = totals[model]
        if counts.total == 0:
            continue
        tokens = tokens + counts
        price = table.resolve(model)
        if price is None:
            basis = table.basis(model)
            subject = model if model is not None else "a model not recorded"
            unknown.append(f"no price for {subject} ({basis})")
            lines.append(ModelLine(model, counts, None, basis, None, None))
            continue
        per_kind = price.kind_costs(counts)
        cost = sum(per_kind.values(), Decimal(0))
        for name, value in per_kind.items():
            kinds[name] += value
        partial += cost
        lines.append(ModelLine(model, counts, price, table.basis(model), cost, per_kind))
    return PricedTotals(
        tokens=tokens,
        models=tuple(lines),
        cost_usd=None if unknown else partial,
        partial_usd=partial,
        kind_usd=kinds,
        unknown_reason="; ".join(unknown) if unknown else None,
    )


def to_usd(value: Decimal | None) -> float | None:
    """The float the snapshot and the card state carry, rounded to micro-dollars. The sums stay exact."""
    return None if value is None else float(value.quantize(MICRO_DOLLAR))


_usd = to_usd


def _tokens_json(counts: UsageCounts) -> dict[str, int]:
    return {
        "input": counts.input_tokens,
        "output": counts.output_tokens,
        "cache_read": counts.cache_read,
        "cache_write": counts.cache_write,
        "total": counts.total,
    }


def usage_summary(totals: Mapping[str | None, UsageCounts], table: PriceTable) -> dict[str, Any]:
    """The JSON the dashboard renders: tokens by kind, cost with its parts, models, pricing date."""
    priced = price_totals(totals, table)
    return {
        "tokens": _tokens_json(priced.tokens),
        "cost": {
            "usd": _usd(priced.cost_usd),
            "partial_usd": _usd(priced.partial_usd),
            "kind_usd": {f"{name}_usd": _usd(value) for name, value in priced.kind_usd.items()},
            "unknown_reason": priced.unknown_reason,
        },
        "models": [
            {
                "model": line.model,
                "tokens": _tokens_json(line.tokens),
                "priced": line.cost_usd is not None,
                "price": None if line.price is None else {
                    "input_per_million": line.price.input_per_million,
                    "output_per_million": line.price.output_per_million,
                    "cache_read_per_million": line.price.cache_read_per_million,
                    "cache_write_per_million": line.price.cache_write_per_million,
                },
                "cost_usd": _usd(line.cost_usd),
                "reason": None if line.cost_usd is not None else line.basis,
            }
            for line in priced.models
        ],
        "pricing": {
            "as_of": table.as_of.isoformat() if table.as_of is not None else None,
            "source": table.source,
        },
    }


def usage_block(
    ledger: TokenLedger, table: PriceTable, *, today: date, symphonies: Iterable[str] = (),
) -> dict[str, Any]:
    """The top-level ``token_usage`` snapshot block (tracked): today and the rolling UTC week.

    139: ``by_symphony`` repeats the same two figures for each named symphony, so the
    dashboard can show one symphony's spend without a second request.
    """
    today_iso = today.isoformat()
    week_start = (today - timedelta(days=WEEK_DAYS - 1)).isoformat()

    def _figures(folder: str) -> dict[str, Any]:
        return {
            "today": usage_summary(ledger.totals(symphony=folder, days=(today_iso, today_iso)), table),
            "week": usage_summary(ledger.totals(symphony=folder, days=(week_start, today_iso)), table),
        }

    # 139 review: a symphony's figures are looked up by the folder its runs were written
    # to (the same sanitised name), and the runs that name no symphony are reported
    # apart, so the parts plus the remainder always add up to the total.
    return {
        "tracked": True,
        "reason": None,
        "day": today_iso,
        "week_start": week_start,
        "today": usage_summary(ledger.totals(days=(today_iso, today_iso)), table),
        "week": usage_summary(ledger.totals(days=(week_start, today_iso)), table),
        "by_symphony": {name: _figures(sanitize_segment(name)) for name in symphonies},
        "unattributed": _figures(UNATTRIBUTED),
        # 131: the conductor's own model calls, by the folder they were written to.
        # The conductor's calls that name no symphony. Calls that do are in their symphony's figures.
        "conductor": _figures(CONDUCTOR_FOLDER),
    }


def disabled_usage_block(*, reason: str) -> dict[str, Any]:
    """The block when transcripts are off: nothing is tracked, so nothing is priced."""
    return {
        "tracked": False,
        "reason": reason,
        "day": None,
        "week_start": None,
        "today": None,
        "week": None,
    }


def session_usage(
    ledger: TokenLedger,
    table: PriceTable,
    *,
    run_id: str,
    card: str,
) -> dict[str, Any]:
    """One active session's figures: this run, and every run of its card."""
    return {
        "tracked": True,
        "run": usage_summary(ledger.totals(run_id=run_id), table),
        "card": usage_summary(ledger.totals(card=card), table),
    }
