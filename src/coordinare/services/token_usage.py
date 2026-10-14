"""Token and cost accounting from transcript ``usage`` records (spec 134).

The source of truth is the ``usage`` records the daemon accepts onto the run
transcript (spec 129). The writer hands every accepted record to :class:`TokenLedger`
(``observe``), so totals rise on the poll that absorbs a record. After a restart the
ledger is rebuilt from the transcript files inside the window (``load_from_transcripts``),
and the writer continues from the cursor the file holds, so nothing is counted twice.

Per run, one tier of records is the total (see :meth:`TokenLedger.run_buckets`):

1. ``message`` records, when any exist. These are per model message and carry the model.
2. otherwise the last ``run`` record. It is cumulative, so it replaces, not adds.
3. otherwise ``scope``-less records, summed, and the run is flagged ``unscoped``.

Cost comes from a per-model price table (:class:`PriceTable`), built from the
``cost_tracking`` config. A model with no price keeps its token counts and its cost
is reported unknown, with the reason. No price is ever guessed.

Day totals are keyed by the UTC day of each record's timestamp.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

    from coordinare.config import PriceRates

logger = structlog.get_logger(__name__)

# The ledger keeps the 7-day week plus one day of slack. Older runs are pruned.
USAGE_WINDOW_DAYS = 8
WEEK_DAYS = 7
# Per-turn entries kept for one run. Totals stay exact beyond this bound.
MAX_TURNS_PER_RUN = 2000

_SCOPES: frozenset[str] = frozenset({"message", "run"})
_USAGE_TYPE = "usage"
_USAGE_MARKER = b'"usage"'
_TRANSCRIPT_SUFFIX = ".jsonl"
_NO_PRICE = "no price configured"
_NO_MODEL = "model not recorded in the usage record"
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
    symphony: str = ""
    card: str = ""
    stage: str = ""
    last_seq: int = 0
    last_day: str = ""
    turns: list[UsageTurn] = field(default_factory=list)
    turns_dropped: bool = False
    message: dict[tuple[str, str | None], UsageCounts] = field(default_factory=dict)
    unscoped_buckets: dict[tuple[str, str | None], UsageCounts] = field(default_factory=dict)
    run_bucket: tuple[tuple[str, str | None], UsageCounts] | None = None

    def buckets(self) -> dict[tuple[str, str | None], UsageCounts]:
        """The tier rule: message, else the last run record, else unscoped."""
        if self.message:
            return self.message
        if self.run_bucket is not None:
            key, counts = self.run_bucket
            return {key: counts}
        return self.unscoped_buckets

    def info(self) -> RunInfo:
        return RunInfo(
            run_id=self.run_id,
            symphony=self.symphony,
            card=self.card,
            stage=self.stage,
            last_seq=self.last_seq,
            unscoped=not self.message and self.run_bucket is None and bool(self.unscoped_buckets),
            turns_dropped=self.turns_dropped,
        )


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
        self._lock = threading.Lock()

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
        counted = 0
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                run = _Run(run_id=run_id)
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
        key = (day, model)
        if scope == "run":
            run.run_bucket = (key, counts)  # cumulative: the latest run record is the total
            return True
        if scope == "message":
            run.message[key] = run.message.get(key, UsageCounts()) + counts
        else:
            run.unscoped_buckets[key] = run.unscoped_buckets.get(key, UsageCounts()) + counts
        if len(run.turns) < MAX_TURNS_PER_RUN:
            run.turns.append(UsageTurn(seq=seq, day=day, model=model, scope=scope, counts=counts))
        else:
            run.turns_dropped = True
        return True

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
            return dict(out)

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
        """Forget runs whose newest record is older than the window. Returns how many."""
        cutoff = (today - timedelta(days=self._window_days - 1)).isoformat()
        with self._lock:
            stale = [rid for rid, run in self._runs.items() if run.last_day and run.last_day < cutoff]
            for rid in stale:
                del self._runs[rid]
            if stale:
                self._version += 1
                self._cache.clear()
            return len(stale)

    def load_from_transcripts(self, root: Path, *, now: datetime) -> int:
        """Rebuild from ``<root>/<symphony>/<card>/<run>.jsonl`` files modified inside the window.

        Only ``usage`` lines are parsed. Symphony and card come from the folder names
        (already sanitized by the writer), and the stage from each record's ``stage`` key.
        Returns how many usage records were counted. A missing root counts nothing.
        """
        if not root.is_dir():
            return 0
        window_start = (now - timedelta(days=self._window_days)).timestamp()
        counted = 0
        for path in _run_files(root):
            try:
                if path.stat().st_mtime < window_start:
                    continue
            except OSError:
                continue
            counted += self.observe(
                path.stem,
                symphony=path.parent.parent.name,
                card=path.parent.name,
                stage="",
                records=_usage_lines(path),
            )
        self.prune(now.date())
        logger.info("token_usage.rebuilt", root=str(root), usage_records=counted, runs=len(self._runs))
        return counted


def _run_files(root: Path) -> list[Path]:
    """Run files two folders down (symphony, then card), in name order. Links are skipped."""
    found: list[Path] = []
    for symphony in sorted(root.iterdir()):
        if symphony.is_symlink() or not symphony.is_dir():
            continue
        for card in sorted(symphony.iterdir()):
            if card.is_symlink() or not card.is_dir():
                continue
            for entry in sorted(card.iterdir()):
                if entry.suffix == _TRANSCRIPT_SUFFIX and entry.is_file() and not entry.is_symlink():
                    found.append(entry)
    return found


def _usage_lines(path: Path) -> list[dict[str, Any]]:
    """The usage records of one transcript file. Torn or foreign lines are skipped."""
    records: list[dict[str, Any]] = []
    try:
        with path.open("rb") as handle:
            for line in handle:
                if _USAGE_MARKER not in line:
                    continue
                try:
                    parsed = json.loads(line)
                except ValueError:
                    continue
                if isinstance(parsed, dict) and parsed.get("type") == _USAGE_TYPE:
                    records.append(parsed)
    except OSError:
        return []
    return records


# --- price table ---------------------------------------------------------------------


@dataclass(frozen=True)
class ModelPrice:
    """Dollars per million tokens, for each kind."""

    input_per_million: float
    output_per_million: float
    cache_read_per_million: float
    cache_write_per_million: float

    def kind_costs(self, counts: UsageCounts) -> dict[str, float]:
        scale = 1_000_000
        return {
            "input": counts.input_tokens / scale * self.input_per_million,
            "output": counts.output_tokens / scale * self.output_per_million,
            "cache_read": counts.cache_read / scale * self.cache_read_per_million,
            "cache_write": counts.cache_write / scale * self.cache_write_per_million,
        }


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
    cost_usd: float | None
    kind_usd: dict[str, float] | None


@dataclass(frozen=True)
class PricedTotals:
    tokens: UsageCounts
    models: tuple[ModelLine, ...]
    cost_usd: float | None
    partial_usd: float
    kind_usd: dict[str, float]
    unknown_reason: str | None


def price_totals(totals: Mapping[str | None, UsageCounts], table: PriceTable) -> PricedTotals:
    """Price one set of model totals. ``cost_usd`` is None when any token is unpriced."""
    tokens = UsageCounts()
    lines: list[ModelLine] = []
    kinds: dict[str, float] = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}
    partial = 0.0
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
        cost = sum(per_kind.values())
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
            "usd": None if priced.cost_usd is None else round(priced.cost_usd, 12),
            "partial_usd": round(priced.partial_usd, 12),
            "kind_usd": {f"{name}_usd": round(value, 12) for name, value in priced.kind_usd.items()},
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
                "cost_usd": None if line.cost_usd is None else round(line.cost_usd, 12),
                "reason": None if line.cost_usd is not None else line.basis,
            }
            for line in priced.models
        ],
        "pricing": {
            "as_of": table.as_of.isoformat() if table.as_of is not None else None,
            "source": table.source,
        },
    }


def usage_block(ledger: TokenLedger, table: PriceTable, *, today: date) -> dict[str, Any]:
    """The top-level ``token_usage`` snapshot block (tracked): today and the rolling UTC week."""
    today_iso = today.isoformat()
    week_start = (today - timedelta(days=WEEK_DAYS - 1)).isoformat()
    return {
        "tracked": True,
        "reason": None,
        "day": today_iso,
        "week_start": week_start,
        "today": usage_summary(ledger.totals(days=(today_iso, today_iso)), table),
        "week": usage_summary(ledger.totals(days=(week_start, today_iso)), table),
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
