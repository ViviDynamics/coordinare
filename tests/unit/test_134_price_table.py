"""#134: the per-model price table, its validation, matching and deprecated fallback."""
from __future__ import annotations

from datetime import date
from typing import Any

import pytest
import structlog
from pydantic import ValidationError

from coordinare.config import CostTrackingConfig
from coordinare.services.token_usage import PriceTable, price_table_from_config

OPUS: dict[str, float] = {
    "input_per_million": 15.0,
    "output_per_million": 75.0,
    "cache_read_per_million": 1.5,
    "cache_write_per_million": 18.75,
}
HAIKU: dict[str, float] = {
    "input_per_million": 1.0,
    "output_per_million": 5.0,
    "cache_read_per_million": 0.1,
    "cache_write_per_million": 1.25,
}


def _table(*entries: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"pricing_as_of": "2026-10-14", "prices": list(entries), **extra}


def _config(raw: dict[str, Any]) -> CostTrackingConfig:
    return CostTrackingConfig.model_validate(raw)


# --- validation -----------------------------------------------------------------


def test_a_well_formed_table_parses_with_its_date() -> None:
    cfg = _config(_table({"models": ["claude-opus-4-5"], **OPUS}))
    assert cfg.pricing_as_of == date(2026, 10, 14)
    assert cfg.prices[0].models == ["claude-opus-4-5"]
    assert cfg.prices[0].cache_write_per_million == 18.75


def test_prices_without_a_pricing_date_are_refused() -> None:
    with pytest.raises(ValidationError, match="pricing_as_of"):
        _config({"prices": [{"models": ["m"], **OPUS}]})


def test_a_default_price_also_needs_the_pricing_date() -> None:
    with pytest.raises(ValidationError, match="pricing_as_of"):
        _config({"default_price": OPUS})


@pytest.mark.parametrize("field", list(OPUS))
def test_a_negative_rate_is_refused(field: str) -> None:
    with pytest.raises(ValidationError):
        _config(_table({"models": ["m"], **OPUS, field: -0.01}))


@pytest.mark.parametrize("field", list(OPUS))
def test_every_rate_is_required(field: str) -> None:
    entry = {"models": ["m"], **OPUS}
    del entry[field]
    with pytest.raises(ValidationError):
        _config(_table(entry))


def test_an_entry_must_name_at_least_one_model() -> None:
    with pytest.raises(ValidationError):
        _config(_table({"models": [], **OPUS}))


@pytest.mark.parametrize("pattern", ["claude-*-5", "*", "*opus", "claude-**", "claude opus", ""])
def test_a_malformed_pattern_is_refused(pattern: str) -> None:
    with pytest.raises(ValidationError):
        _config(_table({"models": [pattern], **OPUS}))


def test_a_model_listed_twice_is_refused() -> None:
    with pytest.raises(ValidationError, match="claude-opus-4-5"):
        _config(_table(
            {"models": ["claude-opus-4-5"], **OPUS},
            {"models": ["claude-opus-4-5", "alias"], **HAIKU},
        ))


def test_unknown_keys_are_refused() -> None:
    with pytest.raises(ValidationError):
        _config(_table({"models": ["m"], **OPUS, "currency": "USD"}))


# --- matching ---------------------------------------------------------------------


def test_an_exact_id_and_an_alias_resolve_to_their_entry() -> None:
    table = price_table_from_config(_config(_table(
        {"models": ["claude-opus-4-5", "opus"], **OPUS},
    )))
    exact = table.resolve("claude-opus-4-5")
    assert exact is not None and exact.input_per_million == 15.0
    assert table.resolve("opus") is not None


def test_a_prefix_pattern_matches_dated_ids() -> None:
    table = price_table_from_config(_config(_table({"models": ["claude-haiku-4-5*"], **HAIKU})))
    assert table.resolve("claude-haiku-4-5-20251001") is not None
    assert table.resolve("claude-haiku-4-5") is not None
    assert table.resolve("claude-opus-4-5") is None


def test_the_longest_matching_prefix_wins() -> None:
    table = price_table_from_config(_config(_table(
        {"models": ["claude-*"], **HAIKU},
        {"models": ["claude-opus-4-5-*"], **OPUS},
    )))
    price = table.resolve("claude-opus-4-5-20250929")
    assert price is not None and price.output_per_million == 75.0


def test_an_exact_id_beats_a_prefix() -> None:
    table = price_table_from_config(_config(_table(
        {"models": ["claude-opus-4-5-*"], **HAIKU},
        {"models": ["claude-opus-4-5-20250929"], **OPUS},
    )))
    price = table.resolve("claude-opus-4-5-20250929")
    assert price is not None and price.output_per_million == 75.0


def test_an_unmatched_model_is_unknown_and_never_guessed() -> None:
    table = price_table_from_config(_config(_table({"models": ["claude-opus-4-5"], **OPUS})))
    assert table.resolve("gpt-unknown") is None
    assert table.basis("gpt-unknown") == "no price configured"


def test_an_unrecorded_model_is_never_priced_by_a_prefix_or_the_default() -> None:
    table = price_table_from_config(_config(_table(
        {"models": ["claude-*"], **OPUS},
        default_price=HAIKU,
    )))
    assert table.resolve(None) is None


def test_default_price_is_the_operator_chosen_fallback_for_unmatched_models() -> None:
    table = price_table_from_config(_config(_table(
        {"models": ["claude-opus-4-5"], **OPUS},
        default_price=HAIKU,
    )))
    assert table.resolve("claude-opus-4-5") is not None
    fallback = table.resolve("some-other-model")
    assert fallback is not None and fallback.input_per_million == 1.0
    assert table.basis("some-other-model") == "default price"


def test_the_pricing_date_is_carried_on_the_table() -> None:
    table = price_table_from_config(_config(_table({"models": ["m"], **OPUS})))
    assert table.as_of == date(2026, 10, 14)
    assert table.source == "table"


# --- deprecated flat rate ---------------------------------------------------------


def test_the_flat_rate_still_prices_every_model_when_no_table_is_set() -> None:
    table = price_table_from_config(_config({"cost_per_million_tokens": 3.0}))
    price = table.resolve("anything")
    assert price is not None
    assert (price.input_per_million, price.output_per_million) == (3.0, 3.0)
    assert (price.cache_read_per_million, price.cache_write_per_million) == (3.0, 3.0)
    assert table.as_of is None
    assert table.source == "deprecated_flat_rate"
    assert table.resolve(None) is not None  # the flat rate was always one rate for everything


def test_the_flat_rate_logs_a_deprecation_warning_naming_the_replacement() -> None:
    with structlog.testing.capture_logs() as logs:
        _config({"cost_per_million_tokens": 3.0})
    events = [entry for entry in logs if entry["event"] == "config.cost_per_million_tokens_deprecated"]
    assert len(events) == 1
    assert events[0]["log_level"] == "warning"
    assert "prices" in events[0]["replacement"]


def test_a_configured_table_ignores_the_flat_rate_with_a_warning() -> None:
    with structlog.testing.capture_logs() as logs:
        cfg = _config(_table({"models": ["claude-opus-4-5"], **OPUS}, cost_per_million_tokens=3.0))
    table = price_table_from_config(cfg)
    assert table.source == "table"
    assert table.resolve("other") is None
    assert any(e["event"] == "config.cost_per_million_tokens_ignored" for e in logs)


def test_no_pricing_at_all_prices_nothing() -> None:
    table = price_table_from_config(_config({}))
    assert isinstance(table, PriceTable)
    assert table.source == "none"
    assert table.resolve("claude-opus-4-5") is None
    assert table.basis("claude-opus-4-5") == "no price configured"


def test_the_old_default_rate_is_no_longer_invented() -> None:
    assert CostTrackingConfig().cost_per_million_tokens is None
