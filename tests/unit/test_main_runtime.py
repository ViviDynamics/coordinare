from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import coordinare.__main__ as app_main
from coordinare.daemon import RuntimeExecutionError


def test_create_health_app_exposes_routes() -> None:
    daemon = SimpleNamespace(running=True, state={"phase": "running", "error_count": 2})
    app = app_main._create_health_app(daemon)
    paths = {route.path for route in app.routes}

    assert "/health" in paths
    assert "/ready" in paths
    assert "/metrics" in paths


def test_main_exits_2_on_startup_config_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_main, "configure_logging", lambda *args, **kwargs: None)

    def _raise(_cls, _path: Path):
        raise ValueError("bad config")

    monkeypatch.setattr(app_main.ProjectConfiguration, "from_yaml", classmethod(_raise))
    monkeypatch.setattr(
        app_main.argparse.ArgumentParser,
        "parse_args",
        lambda _self: SimpleNamespace(config=Path("x"), log_level=None, structured_output=False),
    )

    with pytest.raises(SystemExit) as exc_info:
        app_main.main()

    assert exc_info.value.code == 2


def test_main_exits_1_on_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    config = SimpleNamespace(output_mode="human", log_level="info")
    monkeypatch.setattr(app_main.ProjectConfiguration, "from_yaml", classmethod(lambda _cls, _path: config))
    monkeypatch.setattr(app_main, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        app_main.argparse.ArgumentParser,
        "parse_args",
        lambda _self: SimpleNamespace(config=Path("x"), log_level=None, structured_output=False),
    )

    def _raise_runtime(coro):
        coro.close()
        raise RuntimeExecutionError(phase="runtime", step="cycle_execution", cause=RuntimeError("boom"))

    monkeypatch.setattr(app_main.asyncio, "run", _raise_runtime)

    with pytest.raises(SystemExit) as exc_info:
        app_main.main()

    assert exc_info.value.code == 1
