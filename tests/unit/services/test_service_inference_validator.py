"""Unit tests for service_inference.validator (T004)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinare.services.service_inference.templater import RenderedScripts
from coordinare.services.service_inference.validator import validate


def _scripts(
    start_body: str = "echo start-ok",
    stop_body: str = "echo stop-ok",
    health_body: str = "echo health-ok",
) -> RenderedScripts:
    # Minimal valid bash scripts that exit 0.
    return RenderedScripts(
        start=f"#!/usr/bin/env bash\nset -e\n{start_body}\n",
        stop=f"#!/usr/bin/env bash\nset -e\n{stop_body}\n",
        health=f"#!/usr/bin/env bash\nset -e\n{health_body}\n",
    )


def test_validate_happy_path_returns_ok():
    result = validate(_scripts(), health_delay_seconds=0.0)

    assert result.ok is True
    assert result.phase is None
    assert "start-ok" in result.stdout
    assert "health-ok" in result.stdout
    assert "stop-ok" in result.stdout
    assert result.returncode == 0


def test_validate_start_failure_short_circuits_health():
    result = validate(
        _scripts(start_body="echo boom >&2; exit 7"),
        health_delay_seconds=0.0,
    )

    assert result.ok is False
    assert result.phase == "start"
    assert result.returncode == 7
    assert "boom" in result.stderr


def test_validate_health_failure_reports_phase_and_still_runs_stop(tmp_path):
    # Use a dedicated working dir so we can assert stop side-effects.
    flag = tmp_path / "stop-ran"
    scripts = _scripts(
        health_body="exit 9",
        stop_body=f"touch {flag}",
    )

    result = validate(scripts, working_dir=tmp_path, health_delay_seconds=0.0)

    assert result.ok is False
    assert result.phase == "health"
    assert result.returncode == 9
    # Best-effort stop must still have run for cleanup, even though we report health failure.
    assert flag.exists(), "stop script must run after a health failure"


def test_validate_stop_failure_reports_phase():
    result = validate(
        _scripts(stop_body="exit 3"),
        health_delay_seconds=0.0,
    )

    assert result.ok is False
    assert result.phase == "stop"
    assert result.returncode == 3


def test_validate_rejects_invalid_timeout():
    with pytest.raises(ValueError, match="timeout_seconds"):
        validate(_scripts(), timeout_seconds=0)


def test_validate_rejects_per_phase_invalid_timeout():
    for kw in ("start_timeout_seconds", "health_timeout_seconds", "stop_timeout_seconds"):
        with pytest.raises(ValueError, match=kw.replace("_seconds", "")):
            validate(_scripts(), **{kw: 0}, health_delay_seconds=0.0)


def test_validate_applies_per_phase_timeout(tmp_path):
    seen_timeouts: list[float] = []
    real_run = subprocess.run

    def _wrapper(argv, **kwargs):  # type: ignore[no-untyped-def]
        seen_timeouts.append(kwargs.get("timeout", -1.0))
        return real_run(argv, **kwargs)

    with patch("coordinare.services.service_inference.validator.subprocess.run", side_effect=_wrapper):
        validate(
            _scripts(),
            working_dir=tmp_path,
            start_timeout_seconds=10.0,
            health_timeout_seconds=5.0,
            stop_timeout_seconds=20.0,
            health_delay_seconds=0.0,
        )

    assert seen_timeouts == [10.0, 5.0, 20.0]


def test_validate_uses_subprocess_run_with_bash(tmp_path):
    """Confirm we invoke bash on each script — guards against accidental shell=True or no-bash."""

    real_run = subprocess.run
    seen_argv0: list[str] = []

    def _wrapper(argv, **kwargs):  # type: ignore[no-untyped-def]
        seen_argv0.append(argv[0])
        return real_run(argv, **kwargs)

    with patch("coordinare.services.service_inference.validator.subprocess.run", side_effect=_wrapper):
        validate(_scripts(), working_dir=tmp_path, health_delay_seconds=0.0)

    # start + health + stop = 3 invocations, all of bash.
    assert seen_argv0 == ["bash", "bash", "bash"]


def test_validate_passes_env_to_subprocess(tmp_path):
    # Script asserts COORDINARE_TEST is set; if env propagation is broken the script exits 1.
    scripts = RenderedScripts(
        start='#!/usr/bin/env bash\n[ -n "$COORDINARE_TEST" ] || exit 1\necho ok\n',
        stop="#!/usr/bin/env bash\necho ok\n",
        health="#!/usr/bin/env bash\necho ok\n",
    )

    result = validate(
        scripts,
        env={"COORDINARE_TEST": "1"},
        working_dir=tmp_path,
        health_delay_seconds=0.0,
    )

    assert result.ok is True


def test_validate_cleans_up_temp_dir(tmp_path, monkeypatch):
    captured: dict[str, Path] = {}

    real_mkdtemp = __import__("tempfile").mkdtemp

    def _wrapper(*args, **kwargs):  # type: ignore[no-untyped-def]
        path = real_mkdtemp(*args, **kwargs)
        captured["dir"] = Path(path)
        return path

    monkeypatch.setattr(
        "coordinare.services.service_inference.validator.tempfile.mkdtemp", _wrapper
    )

    validate(_scripts(), health_delay_seconds=0.0)

    assert "dir" in captured
    assert not captured["dir"].exists(), "temp dir must be removed after validation"


def test_validate_preserves_working_dir_when_provided(tmp_path):
    validate(_scripts(), working_dir=tmp_path, health_delay_seconds=0.0)

    # When caller supplies a working_dir, validator must not delete it.
    assert tmp_path.exists()
    assert (tmp_path / "services-start.sh").exists()
