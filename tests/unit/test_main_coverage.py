"""Coverage tests for coordinare.__main__ helper functions (012-performer coverage)."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import coordinare.__main__ as app_main
from coordinare.__main__ import (
    _build_circuit_breakers,
    _build_transport,
    _circuit_breaker_from,
    _make_trip_callback,
    _retry_config_from,
    _searched_paths_lines,
)
from coordinare.config import ProjectConfiguration, ServiceCircuitConfig, ServiceRetryConfig
from coordinare.config_validation import ConfigValidationResult
from coordinare.resilience import CircuitBreaker, RetryConfig

# ---------------------------------------------------------------------------
# Helpers shared with test_cli_config_validate.py style
# ---------------------------------------------------------------------------

def _make_args(config: Path | None = None, strict: bool = False) -> argparse.Namespace:
    args = argparse.Namespace()
    args.config = config
    args.strict = strict
    return args


# ---------------------------------------------------------------------------
# _searched_paths_lines — line 104 (explicit path) and line 110 (env var)
# ---------------------------------------------------------------------------


def test_searched_paths_lines_explicit_path_not_found() -> None:
    """Line 104: explicit_path not None → path shown as 'not found'."""
    p = Path("/some/explicit/config.yaml")
    lines = _searched_paths_lines(p)
    assert any(str(p) in line and "not found" in line for line in lines), lines


def test_searched_paths_lines_coordinare_config_path_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Line 110: COORDINARE_CONFIG_PATH env var set → shown as 'not found'."""
    monkeypatch.setenv("COORDINARE_CONFIG_PATH", "/env/path/config.yaml")
    lines = _searched_paths_lines(None)
    assert any("COORDINARE_CONFIG_PATH" in line and "not found" in line for line in lines), lines


# ---------------------------------------------------------------------------
# _cmd_config_validate — lines 183-190 (validation FAILS + warnings present)
# ---------------------------------------------------------------------------


def test_cmd_config_validate_fail_with_warnings_prints_deprecation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lines 184-190: config fails validation AND has deprecated fields → deprecation block printed."""
    monkeypatch.delenv("COORDINARE_GITHUB_TOKEN", raising=False)
    # Write a config that: has a deprecated field (slack_webhook_url) AND is missing github_token
    config_file = tmp_path / "config.yaml"
    config_file.write_text("\n".join([
        'project_name: "Demo"',
        'github_org: "acme"',
        "github_project_number: 12",
        'human_reviewers: ["alice"]',
        "slack_webhook_url: https://hooks.slack.com/x",
        # github_token intentionally omitted
    ]))
    with pytest.raises(SystemExit) as exc_info:
        app_main._cmd_config_validate(_make_args(config=config_file))
    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "DEPRECATION WARNINGS" in out


def test_cmd_config_validate_fail_with_warnings_strict_shows_errors_header(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lines 183-190 with strict=True: 'DEPRECATION ERRORS' header shown on failure+warnings."""
    monkeypatch.delenv("COORDINARE_GITHUB_TOKEN", raising=False)
    config_file = tmp_path / "config.yaml"
    config_file.write_text("\n".join([
        'project_name: "Demo"',
        'github_org: "acme"',
        "github_project_number: 12",
        'human_reviewers: ["alice"]',
        "slack_webhook_url: https://hooks.slack.com/x",
    ]))
    with pytest.raises(SystemExit) as exc_info:
        app_main._cmd_config_validate(_make_args(config=config_file, strict=True))
    assert exc_info.value.code == 1
    out = capsys.readouterr().out
    assert "DEPRECATION ERRORS" in out


# ---------------------------------------------------------------------------
# _retry_config_from — lines 203-208
# ---------------------------------------------------------------------------


def test_retry_config_from_maps_fields_correctly() -> None:
    """Lines 203-208: _retry_config_from converts ServiceRetryConfig fields correctly."""
    src = ServiceRetryConfig(
        attempts=5,
        wait_initial_seconds=1.5,
        wait_max_seconds=45.0,
        wait_jitter_seconds=2.5,
    )
    result = _retry_config_from(src)
    assert isinstance(result, RetryConfig)
    assert result.attempts == 5
    assert result.wait_initial == 1.5
    assert result.wait_max == 45.0
    assert result.wait_jitter == 2.5


# ---------------------------------------------------------------------------
# _circuit_breaker_from — lines 211-217
# ---------------------------------------------------------------------------


def test_circuit_breaker_from_maps_fields_correctly() -> None:
    """Lines 211-217: _circuit_breaker_from converts ServiceCircuitConfig fields correctly."""
    cfg = ServiceCircuitConfig(
        failure_threshold=5,
        recovery_window_seconds=60.0,
        observation_window_seconds=180.0,
    )
    cb = _circuit_breaker_from("github", cfg)
    assert isinstance(cb, CircuitBreaker)
    assert cb.service_name == "github"
    assert cb.failure_threshold == 5
    assert cb.recovery_window == 60.0
    assert cb.observation_window == 180.0


# ---------------------------------------------------------------------------
# _make_trip_callback — lines 223-246
# ---------------------------------------------------------------------------


def test_trip_callback_no_running_loop_does_not_call_dispatch() -> None:
    """Line 241: no running loop → RuntimeError caught, dispatch NOT called."""
    svc = MagicMock()
    svc.dispatch = AsyncMock()
    callback = _make_trip_callback(svc)
    # Called outside any async context — no running event loop
    callback("github", "failure")
    svc.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_trip_callback_with_running_loop_creates_task() -> None:
    """Lines 242-244: running loop → task created, dispatch IS called."""
    svc = MagicMock()
    svc.dispatch = AsyncMock(return_value=None)
    callback = _make_trip_callback(svc)
    callback("github", "too many failures")
    # Yield control so the created task can run
    await asyncio.sleep(0)
    svc.dispatch.assert_awaited_once()


# ---------------------------------------------------------------------------
# _build_circuit_breakers — lines 250-257
# ---------------------------------------------------------------------------


def test_build_circuit_breakers_returns_all_five_keys() -> None:
    """Lines 250-257: _build_circuit_breakers returns dict with 5 named circuit breakers."""
    config = ProjectConfiguration(
        project_name="test",
        github_org="acme",
        github_project_number=1,
        github_token="ghp_fake",
        human_reviewers=["alice"],
    )
    breakers = _build_circuit_breakers(config)
    assert set(breakers.keys()) == {"github", "slack", "smtp", "anthropic", "agent"}
    for name, cb in breakers.items():
        assert isinstance(cb, CircuitBreaker), f"{name} is not a CircuitBreaker"


# ---------------------------------------------------------------------------
# _build_transport — lines 260-270
# ---------------------------------------------------------------------------


def test_build_transport_ssh_raises_not_implemented() -> None:
    """Line 265: 'ssh' transport → SshTransport() raises NotImplementedError (stub transport)."""
    cfg = SimpleNamespace(agent_transport="ssh", agent_executable="", transport_timeout_seconds=30)
    with pytest.raises(NotImplementedError, match="SSH transport"):
        _build_transport(cfg)


def test_build_transport_kubernetes_raises_not_implemented() -> None:
    """Line 267: 'kubernetes' transport → KubernetesTransport() raises NotImplementedError (stub transport)."""
    cfg = SimpleNamespace(agent_transport="kubernetes", agent_executable="", transport_timeout_seconds=30)
    with pytest.raises(NotImplementedError, match="Kubernetes transport"):
        _build_transport(cfg)


def test_build_transport_unknown_raises_value_error() -> None:
    """Line 269: unknown transport → ValueError raised."""
    cfg = SimpleNamespace(agent_transport="unknown_xyz", agent_executable="", transport_timeout_seconds=30)
    with pytest.raises(ValueError, match="Unknown transport"):
        _build_transport(cfg)


# ---------------------------------------------------------------------------
# main() generic Exception path — lines 620-627
# ---------------------------------------------------------------------------


def test_main_exits_2_on_generic_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lines 620-627: generic Exception during asyncio.run → SystemExit(2)."""
    config = SimpleNamespace(
        output_mode="human", log_level="info",
        health_check_timeout_seconds=2, poll_interval_seconds=30,
        optional_subsystems=[],
        notifications=SimpleNamespace(channels=[]),
    )

    passing_result = ConfigValidationResult(passed=True, config_file_path=None, warnings=[])
    monkeypatch.setattr(app_main, "validate_config", lambda *a, **kw: passing_result)
    monkeypatch.setattr(app_main, "ProjectConfiguration", lambda **kw: config)
    monkeypatch.setattr(app_main, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(app_main, "validate_auth_config", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        app_main.argparse.ArgumentParser,
        "parse_args",
        lambda _self: SimpleNamespace(
            config=None, log_level=None, structured_output=False,
            command=None, config_action=None, strict=False,
        ),
    )

    def _raise_generic(coro):
        coro.close()
        raise RuntimeError("unexpected generic failure")

    monkeypatch.setattr(app_main.asyncio, "run", _raise_generic)

    with pytest.raises(SystemExit) as exc_info:
        app_main.main()

    assert exc_info.value.code == 2
