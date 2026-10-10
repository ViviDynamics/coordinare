from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from coordinare.config import CoordinareConfiguration
from coordinare.config_validation import (
    coerce_multi_symphony_raw,
    validate_config,
    wrap_legacy_config,
)


def raw_config(multi: bool):
    raw = {"github_org": "example", "github_token": "test-only-token", "human_reviewers": ["qa"], "project_name": "sample", "github_project_number": 1}
    if multi:
        raw["symphonies"] = [{"name": "sample", "github_project_number": 1}]
    return raw


@pytest.mark.parametrize("multi", [False, True])
def test_loader_preserves_root_dispatcher_settings(multi):
    raw = raw_config(multi)
    raw["dispatcher_dedup"] = {"stall_timeout_seconds": 120, "idle_timeout_retries": 1, "enabled": False}
    loader = coerce_multi_symphony_raw if multi else wrap_legacy_config
    config = CoordinareConfiguration(**loader(raw))
    assert config.dispatcher_dedup.stall_timeout_seconds == 120
    assert config.dispatcher_dedup.idle_timeout_retries == 1
    assert config.dispatcher_dedup.enabled is False
    assert config.global_config.project_name == "sample"
    assert config.symphonies[0].github_project_number == 1


@pytest.mark.parametrize("multi", [False, True])
def test_missing_dispatcher_settings_keep_defaults(multi):
    loader = coerce_multi_symphony_raw if multi else wrap_legacy_config
    config = CoordinareConfiguration(**loader(raw_config(multi)))
    assert config.dispatcher_dedup.stall_timeout_seconds == 0
    assert config.dispatcher_dedup.idle_timeout_retries == 2


@pytest.mark.parametrize("multi", [False, True])
@pytest.mark.parametrize("invalid", [{"stall_timeout_seconds": -1}, {"idle_timout_retries": 1}, None])
def test_invalid_dispatcher_settings_are_rejected_by_loader_and_diagnostic(multi, invalid, tmp_path, monkeypatch):
    raw = raw_config(multi)
    raw["dispatcher_dedup"] = invalid
    loader = coerce_multi_symphony_raw if multi else wrap_legacy_config
    with pytest.raises(ValidationError):
        CoordinareConfiguration(**loader(raw))
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    result = validate_config(path)
    assert result.passed is False
    assert any(error.field_path.startswith("dispatcher_dedup") for error in result.errors)
