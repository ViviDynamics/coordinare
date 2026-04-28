"""Tests for performer_tuning translation service (055 Phase 8)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from coordinare.config import PerformerRoleConfig, PerformersConfig
from coordinare.services.performer_tuning import translate_tuning

# ---------------------------------------------------------------------------
# translate_tuning — effort translation per backend
# ---------------------------------------------------------------------------


def _cfg(**kwargs) -> PerformerRoleConfig:
    return PerformerRoleConfig(**kwargs)


class TestEffortTranslation:
    def test_opencode_effort_passthrough(self):
        result = translate_tuning(_cfg(backend="opencode", effort="high"))
        assert result == {"effort": "high"}

    def test_codex_effort_passthrough(self):
        result = translate_tuning(_cfg(backend="codex", effort="medium"))
        assert result == {"effort": "medium"}

    def test_anthropic_effort_low_to_thinking_budget(self):
        result = translate_tuning(_cfg(backend="anthropic", effort="low"))
        assert result == {"thinking": {"type": "enabled", "budget_tokens": 1_024}}

    def test_claude_effort_medium_to_thinking_budget(self):
        result = translate_tuning(_cfg(backend="claude", effort="medium"))
        assert result == {"thinking": {"type": "enabled", "budget_tokens": 8_192}}

    def test_anthropic_effort_high_to_thinking_budget(self):
        result = translate_tuning(_cfg(backend="anthropic", effort="high"))
        assert result == {"thinking": {"type": "enabled", "budget_tokens": 32_000}}

    def test_claude_code_effort_to_thinking_budget(self):
        result = translate_tuning(_cfg(backend="claude_code", effort="high"))
        assert result == {"thinking": {"type": "enabled", "budget_tokens": 32_000}}

    def test_unknown_backend_effort_passthrough_with_warning(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING):
            result = translate_tuning(_cfg(backend="mystery", effort="low"))
        assert result == {"effort": "low"}

    def test_effort_none_omitted(self):
        result = translate_tuning(_cfg(backend="opencode", effort=None))
        assert "effort" not in result
        assert "thinking" not in result


class TestTemperatureTranslation:
    def test_temperature_included_when_set(self):
        result = translate_tuning(_cfg(backend="opencode", temperature=0.3))
        assert result["temperature"] == 0.3

    def test_temperature_none_omitted(self):
        result = translate_tuning(_cfg(backend="opencode"))
        assert "temperature" not in result

    def test_temperature_zero_included(self):
        result = translate_tuning(_cfg(backend="codex", temperature=0.0))
        assert result["temperature"] == 0.0


class TestMaxTokensTranslation:
    def test_max_tokens_included_when_set(self):
        result = translate_tuning(_cfg(backend="opencode", max_tokens=8192))
        assert result["max_tokens"] == 8192

    def test_max_tokens_none_omitted(self):
        result = translate_tuning(_cfg(backend="opencode"))
        assert "max_tokens" not in result


class TestCombinedTranslation:
    def test_all_fields_set(self):
        cfg = _cfg(backend="codex", effort="high", temperature=0.2, max_tokens=32768)
        result = translate_tuning(cfg)
        assert result == {"effort": "high", "temperature": 0.2, "max_tokens": 32768}

    def test_all_none_returns_empty(self):
        result = translate_tuning(_cfg(backend="opencode"))
        assert result == {}


# ---------------------------------------------------------------------------
# PerformersConfig.resolved_role — default inheritance
# ---------------------------------------------------------------------------


class TestResolvedRole:
    def test_no_default_returns_role_as_is(self):
        cfg = PerformersConfig(implementer=PerformerRoleConfig(backend="codex", effort="high"))
        resolved = cfg.resolved_role("implementer")
        assert resolved is not None
        assert resolved.backend == "codex"
        assert resolved.effort == "high"

    def test_role_none_returns_none(self):
        cfg = PerformersConfig(default=PerformerRoleConfig(backend="codex"))
        assert cfg.resolved_role("implementer") is None

    def test_role_inherits_default_backend(self):
        cfg = PerformersConfig(
            default=PerformerRoleConfig(backend="codex", effort="medium", max_tokens=16384),
            implementer=PerformerRoleConfig(),
        )
        resolved = cfg.resolved_role("implementer")
        assert resolved is not None
        assert resolved.backend == "codex"
        assert resolved.effort == "medium"
        assert resolved.max_tokens == 16384

    def test_role_overrides_default_effort(self):
        cfg = PerformersConfig(
            default=PerformerRoleConfig(backend="codex", effort="medium", max_tokens=16384),
            implementer=PerformerRoleConfig(effort="high", max_tokens=32768),
        )
        resolved = cfg.resolved_role("implementer")
        assert resolved is not None
        assert resolved.effort == "high"
        assert resolved.max_tokens == 32768
        assert resolved.backend == "codex"  # inherited

    def test_default_does_not_affect_none_roles(self):
        cfg = PerformersConfig(
            default=PerformerRoleConfig(backend="codex"),
            implementer=None,
        )
        assert cfg.resolved_role("implementer") is None

    def test_unknown_role_name_returns_none(self):
        cfg = PerformersConfig(default=PerformerRoleConfig(backend="codex"))
        assert cfg.resolved_role("nonexistent") is None

    def test_max_tokens_zero_overrides_default(self):
        cfg = PerformersConfig(
            default=PerformerRoleConfig(backend="codex", max_tokens=8192),
            implementer=PerformerRoleConfig(max_tokens=0),
        )
        resolved = cfg.resolved_role("implementer")
        assert resolved is not None
        assert resolved.max_tokens is None  # sentinel 0 → None (unlimited)

    def test_max_tokens_none_inherits_default(self):
        cfg = PerformersConfig(
            default=PerformerRoleConfig(backend="codex", max_tokens=8192),
            implementer=PerformerRoleConfig(),
        )
        resolved = cfg.resolved_role("implementer")
        assert resolved is not None
        assert resolved.max_tokens == 8192  # unset → inherits default


# ---------------------------------------------------------------------------
# PerformerRoleConfig field validators
# ---------------------------------------------------------------------------


class TestFieldValidators:
    def test_temperature_out_of_range_raises(self):
        with pytest.raises(ValidationError):
            PerformerRoleConfig(temperature=1.5)

    def test_temperature_negative_raises(self):
        with pytest.raises(ValidationError):
            PerformerRoleConfig(temperature=-0.1)

    def test_temperature_zero_valid(self):
        cfg = PerformerRoleConfig(temperature=0.0)
        assert cfg.temperature == 0.0

    def test_temperature_one_valid(self):
        cfg = PerformerRoleConfig(temperature=1.0)
        assert cfg.temperature == 1.0

    def test_max_tokens_zero_is_unlimited_sentinel(self):
        cfg = PerformerRoleConfig(max_tokens=0)
        assert cfg.max_tokens == 0  # 0 = unlimited sentinel, resolved to None by resolved_role()

    def test_max_tokens_negative_raises(self):
        with pytest.raises(ValidationError):
            PerformerRoleConfig(max_tokens=-1)

    def test_max_tokens_positive_valid(self):
        cfg = PerformerRoleConfig(max_tokens=1)
        assert cfg.max_tokens == 1
