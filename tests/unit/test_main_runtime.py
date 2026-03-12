from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import coordinare.__main__ as app_main
from coordinare.config_validation import ConfigValidationResult
from coordinare.daemon import RuntimeExecutionError


def test_create_health_app_exposes_routes() -> None:
    daemon = SimpleNamespace(running=True, state={"phase": "running", "error_count": 2})
    app = app_main._create_health_app(daemon)
    paths = {route.path for route in app.routes}

    assert "/health" in paths
    assert "/ready" in paths
    assert "/metrics" in paths


def test_main_exits_2_on_startup_config_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Config validation failure → SystemExit(2) without starting the daemon."""
    monkeypatch.setattr(app_main, "configure_logging", lambda *args, **kwargs: None)

    # Simulate validate_config returning a failed result
    failed_result = ConfigValidationResult(passed=False)
    monkeypatch.setattr(app_main, "validate_config", lambda *a, **kw: failed_result)

    monkeypatch.setattr(
        app_main.argparse.ArgumentParser,
        "parse_args",
        lambda _self: SimpleNamespace(
            config=Path("x"), log_level=None, structured_output=False,
            command=None, config_action=None, strict=False,
        ),
    )

    with pytest.raises(SystemExit) as exc_info:
        app_main.main()

    assert exc_info.value.code == 2


def test_main_exits_1_on_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """RuntimeExecutionError during daemon run → SystemExit(1)."""
    config = SimpleNamespace(
        output_mode="human", log_level="info",
        health_check_timeout_seconds=2, poll_interval_seconds=30,
        optional_subsystems=[],
        notifications=SimpleNamespace(channels=[]),
    )

    # Simulate validate_config returning a passing result (env-vars-only, no file)
    passing_result = ConfigValidationResult(passed=True, config_file_path=None, warnings=[])
    monkeypatch.setattr(app_main, "validate_config", lambda *a, **kw: passing_result)

    # Mock ProjectConfiguration() constructor (env-vars-only path)
    monkeypatch.setattr(app_main, "ProjectConfiguration", lambda **kw: config)

    monkeypatch.setattr(app_main, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        app_main.argparse.ArgumentParser,
        "parse_args",
        lambda _self: SimpleNamespace(
            config=None, log_level=None, structured_output=False,
            command=None, config_action=None, strict=False,
        ),
    )

    def _raise_runtime(coro):
        coro.close()
        raise RuntimeExecutionError(phase="runtime", step="cycle_execution", cause=RuntimeError("boom"))

    monkeypatch.setattr(app_main.asyncio, "run", _raise_runtime)

    with pytest.raises(SystemExit) as exc_info:
        app_main.main()

    assert exc_info.value.code == 1
