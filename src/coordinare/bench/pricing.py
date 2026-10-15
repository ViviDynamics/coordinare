"""Spec 206: the bench prices runs with the daemon's price table.

The bench and the daemon read one table, ``cost_tracking.prices`` (spec 134), through the
same loader (``price_table_from_config``). A model with no price has unknown cost and the
reason is recorded. No flat rate is ever applied to a model.

A bench dispatch has two token figures. ``tokens_processed`` is the performer's single
total, and a total cannot be priced honestly, because output and cache tokens cost different
amounts. A real-performer dispatch (spec 151) also writes a transcript (spec 129) whose
``usage`` records carry the per-kind counts, and the daemon's ledger (spec 134) keys them by
the dispatch's session id. Spec 242 prices from that ledger exactly (``price_usage``). A
total-only dispatch, with no transcript usage, stays unknown with the split reason.
A stub dispatch makes no model calls, so it reports zero tokens of every kind and is priced
at zero (``BASIS_NO_MODEL_CALLS``).

Unknown is never zero. A dispatch with no token count, no kind split, or a model with no
price yields ``cost_usd=None`` with the reason. Rollups sum only what is priced and say so
when a sum is partial.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coordinare.config import ProjectConfiguration
from coordinare.config_validation import _load_raw_yaml
from coordinare.services.token_usage import (
    PriceTable,
    UsageCounts,
    price_table_from_config,
    price_totals,
    to_usd,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

#: How a cost was derived. Stored on every priced dispatch, card and run.
BASIS_PER_KIND = "exact_per_kind_counts"
#: The dispatch made no model calls, so its zero tokens of every kind price at zero (stub).
BASIS_NO_MODEL_CALLS = "no_model_calls"

NO_TOKENS_REASON = "no token count was reported for this dispatch"
SPLIT_NOT_REPORTED_REASON = "token split by kind not reported"


@dataclass(frozen=True)
class TokenCost:
    """The cost of some tokens, or why it is unknown."""

    cost_usd: float | None
    unknown_reason: str | None
    basis: str | None


def price_counts(counts: UsageCounts, model: str | None, table: PriceTable, *, basis: str) -> TokenCost:
    """Price counts of each token kind with the model's entry: input, output, cache read and
    cache write, each at its own rate. ``basis`` says how the caller obtained the split."""
    priced = price_totals({model or None: counts}, table)
    if priced.cost_usd is None:
        return TokenCost(None, priced.unknown_reason, None)
    return TokenCost(to_usd(priced.cost_usd), None, basis)


def price_usage(usage: Mapping[str | None, UsageCounts], table: PriceTable, *, basis: str) -> TokenCost:
    """Price a dispatch's per-model usage exactly: each kind at its model's own rate (spec 242).

    ``usage`` maps each model to its four counts, as the transcript ledger reports them. A
    model with no price, or no recorded model, makes the whole cost unknown with the reason.
    """
    priced = price_totals(usage, table)
    if priced.cost_usd is None:
        return TokenCost(None, priced.unknown_reason, None)
    return TokenCost(to_usd(priced.cost_usd), None, basis)


def price_dispatch(
    usage: Mapping[str | None, UsageCounts] | None,
    tokens: int | None,
    model: str | None,
    table: PriceTable,
) -> TokenCost:
    """Price one dispatch from its transcript usage when it has any, else from its total (spec 242).

    ``usage`` is None when the dispatch wrote no transcript usage. Its cost then falls back to
    the total, which cannot be priced, and the total's reason says why (the split by kind was
    not reported). An empty mapping is a dispatch that made no model calls (the stub
    substrate): zero of every kind is zero dollars at any rate, and the basis says so.
    """
    if usage is None:
        return price_tokens(tokens, model, table)
    if not usage:
        return TokenCost(0.0, None, BASIS_NO_MODEL_CALLS)
    return price_usage(usage, table, basis=BASIS_PER_KIND)


def price_tokens(tokens: int | None, model: str | None, table: PriceTable) -> TokenCost:
    """Price one dispatch from its total token count alone: never, honestly.

    A total has no per-kind split, and output, cache read and cache write each have their own
    rate, so any single rate would invent a figure. The cost is unknown with the split reason,
    and an unpriced model is named as well. Use ``price_counts`` once the split exists.
    """
    if tokens is None:
        return TokenCost(None, NO_TOKENS_REASON, None)
    reasons = [SPLIT_NOT_REPORTED_REASON]
    priced = price_totals({model or None: UsageCounts(input_tokens=tokens)}, table)
    if priced.cost_usd is None and priced.unknown_reason:
        reasons.append(priced.unknown_reason)
    return TokenCost(None, "; ".join(reasons), None)


def combine(costs: Iterable[TokenCost]) -> TokenCost:
    """Sum dispatch costs into a card cost. Any unknown part makes the whole unknown."""
    items = list(costs)
    if not items:
        return TokenCost(None, NO_TOKENS_REASON, None)
    reasons: list[str] = []
    for item in items:
        if item.unknown_reason is not None and item.unknown_reason not in reasons:
            reasons.append(item.unknown_reason)
    if reasons:
        return TokenCost(None, "; ".join(reasons), None)
    total = round(sum(item.cost_usd or 0.0 for item in items), 6)
    return TokenCost(total, None, items[0].basis)


def bench_price_table(cost_tracking: Any) -> PriceTable:
    """The bench's table from a ``cost_tracking`` block, read by the daemon's own loader.

    Only the price table counts here: ``prices`` or ``default_price``. The deprecated
    ``cost_per_million_tokens`` flat rate is ignored, because it would price every model,
    including an unrecorded one, at one guessed rate. A config with only that flat rate
    therefore leaves every bench cost unknown.
    """
    if cost_tracking is None:
        return price_table_from_config(None)
    if not getattr(cost_tracking, "prices", None) and getattr(cost_tracking, "default_price", None) is None:
        return price_table_from_config(None)
    return price_table_from_config(cost_tracking)


def price_table_for_run(
    *,
    config: Any = None,
    real_config: Any = None,
    config_path: str | Path | None = None,
) -> PriceTable:
    """The table a bench run prices with, from the same config the daemon would load.

    Precedence: a materialized ``CoordinareConfiguration`` (``config``), then a
    ``ProjectConfiguration`` injected for real mode (``real_config``), then the YAML at
    ``config_path`` loaded the way the daemon loads it. With none of these there is no
    table, and every model reads as unpriced.
    """
    if config is not None:
        return bench_price_table(config.global_config.cost_tracking)
    if real_config is not None:
        return bench_price_table(real_config.cost_tracking)
    if config_path and Path(config_path).is_file():
        loaded = ProjectConfiguration(**_load_raw_yaml(Path(config_path)))
        return bench_price_table(loaded.cost_tracking)
    return price_table_from_config(None)
