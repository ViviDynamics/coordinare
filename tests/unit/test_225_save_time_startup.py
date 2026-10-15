"""#225 review: a save cannot write a config the daemon cannot start with, and a cleared secret is absent.

The dashboard token is written as absent when cleared, never as "". A "" that reaches the loader
means unset. The save-time candidate check runs the daemon's own start-time checks, so the page
refuses a clear that would stop the daemon. The Clear and required flags are checked against those
same refusals, for every secret field in the model, found by type.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest
import yaml

from coordinare import __main__ as daemon_main
from coordinare import config_descriptors as cd
from coordinare.config import CoordinareConfiguration, ProjectConfiguration, _holds_secret
from coordinare.config_validation import coerce_multi_symphony_raw
from coordinare.services.config_write_service import (
    SaveRequest,
    _validate_candidate,
    compute_content_hash,
    save_section,
)
from coordinare.start_checks import validate_start_time

_TOKEN = "a-dashboard-token-with-more-than-thirty-two-characters"
_RAW: dict[str, Any] = {
    "github_org": "acme",
    "github_token": "ghp_fixturetoken",
    "human_reviewers": ["alice"],
    "symphonies": [{"name": "demo", "github_project_number": 100}],
}


@pytest.fixture(autouse=True)
def _no_env_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The daemon's own environment can supply the GitHub token; these tests decide without it."""
    monkeypatch.delenv("COORDINARE_GITHUB_TOKEN", raising=False)


def _write(path: Path, raw: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return path


def _save(path: Path, changes: dict[str, Any]) -> Any:
    request = SaveRequest(
        section="global", changes=changes, base_hash=compute_content_hash(path),
    )
    return save_section(request, path)


def _secret_fields() -> list[str]:
    """Every secret field of the global model, found by its type (not a hand-kept list)."""
    return [name for name, info in ProjectConfiguration.model_fields.items() if _holds_secret(info.annotation)]


# --- the cleared secret is written absent, and the daemon starts with it ----------------


def test_a_cleared_dashboard_token_is_written_absent_and_the_daemon_starts(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.yaml", {**_RAW, "dashboard_auth_token": _TOKEN})

    result = _save(path, {"dashboard_auth_token": ""})

    assert result.ok, result.errors
    on_disk = yaml.safe_load(path.read_text())
    assert "dashboard_auth_token" not in on_disk
    loaded = ProjectConfiguration.from_yaml(path)
    assert loaded.dashboard_auth_token is None
    validate_start_time(loaded)  # the start-time checks pass on what was saved


def test_an_empty_token_on_disk_loads_as_unset(tmp_path: Path) -> None:
    """A "" that reaches the loader (an older save, an empty ${VAR}) is unset, not a 0-character token."""
    path = _write(tmp_path / "config.yaml", {**_RAW, "dashboard_auth_token": ""})

    loaded = ProjectConfiguration.from_yaml(path)

    assert loaded.dashboard_auth_token is None
    validate_start_time(loaded)


def test_an_empty_token_is_unset_for_the_model_itself() -> None:
    assert ProjectConfiguration(**{**_RAW, "dashboard_auth_token": "   "}).dashboard_auth_token is None


# --- a clear that would stop the daemon is refused, with the reason --------------------


def test_a_cleared_token_on_a_remote_bind_is_refused_and_nothing_is_written(tmp_path: Path) -> None:
    raw = {**_RAW, "dashboard_host": "0.0.0.0", "dashboard_auth_token": _TOKEN}
    path = _write(tmp_path / "config.yaml", raw)
    before = path.read_text()

    result = _save(path, {"dashboard_auth_token": ""})

    assert not result.ok
    assert any("Non-loopback dashboard_host requires dashboard_auth_token" in e.message for e in result.errors)
    assert path.read_text() == before


def test_a_short_token_is_refused_at_save_with_the_reason(tmp_path: Path) -> None:
    path = _write(tmp_path / "config.yaml", {**_RAW, "dashboard_auth_token": _TOKEN})

    result = _save(path, {"dashboard_auth_token": "too-short"})

    assert not result.ok
    assert any("at least 32 characters" in e.message for e in result.errors)


def test_the_candidate_check_runs_the_same_start_time_checks_as_the_daemon() -> None:
    errors = _validate_candidate({**_RAW, "dashboard_host": "0.0.0.0"})
    assert errors
    assert "Non-loopback dashboard_host requires dashboard_auth_token" in errors[0].message


def test_the_daemon_boot_calls_the_same_start_time_function() -> None:
    assert "validate_start_time(config)" in inspect.getsource(daemon_main._run)


# --- the required flag is the save-time refusal, for every secret field -----------------


_SCENARIOS: dict[str, dict[str, Any]] = {
    "loopback with a token": {"dashboard_host": "127.0.0.1", "dashboard_auth_token": _TOKEN},
    "remote bind with a token": {"dashboard_host": "0.0.0.0", "dashboard_auth_token": _TOKEN},
    "remote bind with no token": {"dashboard_host": "0.0.0.0"},
}


@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
def test_required_flag_matches_the_save_time_refusal_for_every_secret_field(scenario: str) -> None:
    base = {**_RAW, **_SCENARIOS[scenario]}
    checked: set[str] = set()
    for field in _secret_fields():
        if field not in base:
            # Nothing set, so there is nothing to clear: the page shows the field as optional.
            continue
        config = CoordinareConfiguration(**coerce_multi_symphony_raw(base))
        setting = {s.key: s for s in cd.build_section(config, "global").settings}[f"global.{field}"]
        cleared = {k: v for k, v in base.items() if k != field}
        refused = bool(_validate_candidate(cleared))
        assert setting.required is refused, f"{scenario}: {field} required={setting.required}, refused={refused}"
        checked.add(field)
    assert checked, f"no secret field was set in scenario {scenario!r}"


def test_the_secret_fields_are_found_by_type() -> None:
    assert set(_secret_fields()) == {"github_token", "dashboard_auth_token"}
