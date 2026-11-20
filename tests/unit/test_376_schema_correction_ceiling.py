"""Maintainer ruling 2026-11-19: an attempt may allow up to sixteen schema corrections; the default is unchanged."""
from __future__ import annotations

import pytest

from coordinare.config import EnvCacheConfig
from coordinare.services.environment_attempt import REPAIR_LIMITS, AttemptBudget


def test_sixteen_schema_corrections_are_allowed_and_the_default_is_unchanged() -> None:
    assert REPAIR_LIMITS['schema'] == 16
    assert EnvCacheConfig().manifest_max_schema_repairs == 2
    assert EnvCacheConfig(manifest_max_schema_repairs=16).manifest_max_schema_repairs == 16
    assert AttemptBudget(max_schema_repairs=16).max_schema_repairs == 16
    with pytest.raises(ValueError):
        AttemptBudget(max_schema_repairs=17)
