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


# ---------------------------------------------------------------------------
# 048: _build_performer_services with max_concurrency
# ---------------------------------------------------------------------------


def test_build_performer_services_creates_multiple_transports() -> None:
    """048: When max_concurrency > 1, _build_performer_services creates
    multiple service instances per role and stashes the full lists."""
    from unittest.mock import patch

    from coordinare.__main__ import _build_performer_services

    config = MagicMock()
    config.resilience = MagicMock()
    config.resilience.agent_retry = MagicMock()
    config.resilience.agent_retry.max_attempts = 1
    config.resilience.agent_retry.initial_backoff_seconds = 0.1
    config.resilience.agent_retry.max_backoff_seconds = 1.0
    config.resilience.agent_retry.backoff_multiplier = 1.0
    config.resilience.agent_retry.jitter_seconds = 0.0
    config.agent_transport = "subprocess"
    config.agent_executable = "/bin/echo"
    config.transport_timeout_seconds = 30

    # Configure implementer with max_concurrency=2
    impl_config = MagicMock()
    impl_config.transport = None
    impl_config.executable = None
    impl_config.host = None
    impl_config.port = None
    impl_config.timeout_seconds = None
    impl_config.max_concurrency = 2
    config.performers = MagicMock()
    config.performers.implementer = impl_config
    # All other roles return None
    for role in ["advocate", "assessor", "architect", "reviewer", "security", "qa", "tech_writer", "closer"]:
        setattr(config.performers, role, None)

    cbs = _build_circuit_breakers(config)

    with patch("coordinare.__main__._build_transport_for_role") as mock_build:
        mock_build.return_value = MagicMock()
        services = _build_performer_services(config, cbs)

    # Should have created 2 transports for implementer
    assert mock_build.call_count == 2
    assert "implementing" in services
    # Full lists stashed for SlotManager
    lists = getattr(_build_performer_services, "_service_lists", {})
    assert "implementing" in lists
    assert len(lists["implementing"]) == 2


def test_build_performer_services_singleton_clamped() -> None:
    """048: Assessor max_concurrency is clamped to 1 (singleton)."""
    from unittest.mock import patch

    from coordinare.__main__ import _build_performer_services

    config = MagicMock()
    config.resilience = MagicMock()
    config.resilience.agent_retry = MagicMock()
    config.resilience.agent_retry.max_attempts = 1
    config.resilience.agent_retry.initial_backoff_seconds = 0.1
    config.resilience.agent_retry.max_backoff_seconds = 1.0
    config.resilience.agent_retry.backoff_multiplier = 1.0
    config.resilience.agent_retry.jitter_seconds = 0.0
    config.agent_transport = "subprocess"
    config.agent_executable = "/bin/echo"
    config.transport_timeout_seconds = 30

    assessor_config = MagicMock()
    assessor_config.transport = None
    assessor_config.executable = None
    assessor_config.host = None
    assessor_config.port = None
    assessor_config.timeout_seconds = None
    assessor_config.max_concurrency = 5  # should be clamped to 1
    config.performers = MagicMock()
    config.performers.assessor = assessor_config
    for role in ["advocate", "architect", "implementer", "reviewer", "security", "qa", "tech_writer", "closer"]:
        setattr(config.performers, role, None)

    cbs = _build_circuit_breakers(config)

    with patch("coordinare.__main__._build_transport_for_role") as mock_build:
        mock_build.return_value = MagicMock()
        _build_performer_services(config, cbs)

    # Singleton: only 1 transport despite max_concurrency=5
    assert mock_build.call_count == 1


def test_build_performer_services_max_concurrency_zero_skips() -> None:
    """048: max_concurrency=0 disables the role entirely."""
    from unittest.mock import patch

    from coordinare.__main__ import _build_performer_services

    config = MagicMock()
    config.resilience = MagicMock()
    config.resilience.agent_retry = MagicMock()
    config.resilience.agent_retry.max_attempts = 1
    config.resilience.agent_retry.initial_backoff_seconds = 0.1
    config.resilience.agent_retry.max_backoff_seconds = 1.0
    config.resilience.agent_retry.backoff_multiplier = 1.0
    config.resilience.agent_retry.jitter_seconds = 0.0
    config.agent_transport = "subprocess"
    config.agent_executable = "/bin/echo"
    config.transport_timeout_seconds = 30

    impl_config = MagicMock()
    impl_config.max_concurrency = 0  # disabled
    config.performers = MagicMock()
    config.performers.implementer = impl_config
    for role in ["advocate", "assessor", "architect", "reviewer", "security", "qa", "tech_writer", "closer"]:
        setattr(config.performers, role, None)

    cbs = _build_circuit_breakers(config)

    with patch("coordinare.__main__._build_transport_for_role") as mock_build:
        services = _build_performer_services(config, cbs)

    # max_concurrency=0 → no transport created, role skipped
    assert mock_build.call_count == 0
    assert "implementing" not in services


def test_slot_manager_construction_from_service_lists() -> None:
    """048: SlotManager can be built from _service_lists stashed by
    _build_performer_services."""
    from coordinare.services.slot_manager import SlotManager

    # Simulate what main() does after _build_performer_services
    service_lists = {
        "implementing": [MagicMock(), MagicMock()],
        "reviewing": [MagicMock()],
    }
    sm = SlotManager()
    for stage, svc_list in service_lists.items():
        sm.register_pool(stage, svc_list, max_concurrency=len(svc_list))

    assert sm.active_count("implementing") == 0
    assert len(sm.pools["implementing"].services) == 2
    assert len(sm.pools["reviewing"].services) == 1

    # Acquire works
    svc = sm.acquire("implementing", "CARD_A")
    assert svc is not None
    assert sm.active_count("implementing") == 1
