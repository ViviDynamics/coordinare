"""#134 review round 1: the cost_tracking config refuses typos and warns when a budget has no price."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import structlog
from pydantic import ValidationError

from coordinare.config import CostTrackingConfig, ModelPriceConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "config.example.yaml"

RATES: dict[str, float] = {
    "input_per_million": 1.0,
    "output_per_million": 5.0,
    "cache_read_per_million": 0.1,
    "cache_write_per_million": 1.25,
}


def test_an_unknown_key_in_cost_tracking_is_refused_not_ignored() -> None:
    """A typo such as ``price:`` used to load silently and leave the model unpriced."""
    with pytest.raises(ValidationError):
        CostTrackingConfig.model_validate({"price": [{"models": ["m"], **RATES}], "pricing_as_of": "2026-10-01"})


def test_an_unknown_key_in_a_price_entry_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelPriceConfig.model_validate({"models": ["m"], "modles": ["typo"], **RATES})


def test_a_budget_with_no_price_warns_at_load_so_the_alert_is_not_silently_dead() -> None:
    with structlog.testing.capture_logs() as logs:
        CostTrackingConfig.model_validate({"cost_budget_per_card": 5.0})
    events = [entry for entry in logs if entry["event"] == "config.cost_budget_without_price"]
    assert len(events) == 1
    assert events[0]["log_level"] == "warning"


def test_a_budget_with_a_price_table_does_not_warn() -> None:
    with structlog.testing.capture_logs() as logs:
        CostTrackingConfig.model_validate({
            "cost_budget_per_card": 5.0,
            "pricing_as_of": "2026-10-01",
            "prices": [{"models": ["m"], **RATES}],
        })
    assert not [entry for entry in logs if entry["event"] == "config.cost_budget_without_price"]


def _commented_cost_block() -> dict[str, Any]:
    """The example's ``# cost_tracking:`` block with its comment markers removed."""
    import yaml

    lines = EXAMPLE.read_text(encoding="utf-8").splitlines()
    start = lines.index("# cost_tracking:")
    end = next(i for i in range(start, len(lines)) if lines[i].startswith("# Deprecated"))
    block = [line[2:] if line.startswith("# ") else "" for line in lines[start:end]]
    parsed = yaml.safe_load("\n".join(block))
    assert isinstance(parsed, dict)
    return parsed["cost_tracking"]


def test_the_example_cost_block_still_loads_with_the_strict_schema() -> None:
    raw = _commented_cost_block()
    config = CostTrackingConfig.model_validate(raw)
    assert config.pricing_as_of is not None
    assert len(config.prices) == 2
