"""Contract tests for persona_check_map + ci_gate config schema (spec 075)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coordinare.config import (
    CIGateConfig,
    PersonaCheckMapConfig,
    PersonaScopeConfig,
)

# ---------------------------------------------------------------------------
# CIGateConfig — bounds + defaults
# ---------------------------------------------------------------------------


def test_ci_gate_defaults_disabled() -> None:
    cfg = CIGateConfig()
    assert cfg.enabled is False
    assert cfg.max_bounces_per_head == 3
    assert cfg.pending_timeout_seconds == 900


def test_ci_gate_max_bounces_below_one_raises() -> None:
    with pytest.raises(ValidationError):
        CIGateConfig(max_bounces_per_head=0)


def test_ci_gate_max_bounces_above_twenty_raises() -> None:
    with pytest.raises(ValidationError):
        CIGateConfig(max_bounces_per_head=100)


def test_ci_gate_pending_timeout_below_min_raises() -> None:
    with pytest.raises(ValidationError):
        CIGateConfig(pending_timeout_seconds=30)


def test_ci_gate_pending_timeout_above_max_raises() -> None:
    with pytest.raises(ValidationError):
        CIGateConfig(pending_timeout_seconds=8000)


# ---------------------------------------------------------------------------
# PersonaCheckMapConfig — shape + unknown-depth rejection
# ---------------------------------------------------------------------------


def test_persona_check_map_parses_valid_structure() -> None:
    cfg = PersonaCheckMapConfig.model_validate(
        {
            "implementer": {
                "any": ["lint*", "unit-tests*"],   # 077 FR-013 depth-agnostic
                "skim": ["lint*"],
                "normal": ["lint*", "unit-tests*"],
                "full": ["lint*", "unit-tests*", "integration*", "e2e*"],
            },
            "reviewer": {"normal": ["lint*", "unit-tests*"]},
        }
    )
    assert "implementer" in cfg.root
    assert cfg.root["implementer"].any == ["lint*", "unit-tests*"]
    assert cfg.root["implementer"].skim == ["lint*"]
    assert cfg.root["reviewer"].normal == ["lint*", "unit-tests*"]
    assert cfg.root["reviewer"].any == []  # defaults empty when omitted
    assert cfg.root["reviewer"].skim == []
    assert cfg.root["reviewer"].full == []


def test_persona_check_map_any_only_parses() -> None:
    """077 FR-013: an `any`-only map (no depth keys) is valid — the standalone
    gate-scoping shape used without 074 tiering."""
    cfg = PersonaCheckMapConfig.model_validate(
        {"implementer": {"any": ["Validate version", "Unit tests*"]}}
    )
    assert cfg.root["implementer"].any == ["Validate version", "Unit tests*"]


def test_persona_check_map_rejects_non_dict_root() -> None:
    with pytest.raises(ValidationError):
        PersonaCheckMapConfig.model_validate("string")


def test_persona_check_map_rejects_non_list_globs() -> None:
    with pytest.raises(ValidationError):
        PersonaCheckMapConfig.model_validate(
            {"implementer": {"skim": "lint"}}
        )


def test_persona_check_map_rejects_unknown_depth() -> None:
    with pytest.raises(ValidationError):
        PersonaCheckMapConfig.model_validate(
            {"implementer": {"fast": ["lint*"]}}
        )


# ---------------------------------------------------------------------------
# PersonaScopeConfig — null ci_gate treated as default
# ---------------------------------------------------------------------------


def test_persona_scope_with_default_ci_gate() -> None:
    cfg = PersonaScopeConfig(enabled=True)
    assert cfg.ci_gate.enabled is False
    assert cfg.persona_check_map is None


def test_persona_scope_with_full_75_config() -> None:
    cfg = PersonaScopeConfig.model_validate(
        {
            "enabled": True,
            "persona_check_map": {
                "implementer": {"normal": ["lint*"]},
            },
            "ci_gate": {
                "enabled": True,
                "max_bounces_per_head": 5,
                "pending_timeout_seconds": 600,
            },
        }
    )
    assert cfg.ci_gate.enabled is True
    assert cfg.ci_gate.max_bounces_per_head == 5
    assert cfg.persona_check_map is not None
    assert cfg.persona_check_map.root["implementer"].normal == ["lint*"]
