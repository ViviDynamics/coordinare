"""Unit tests for service_inference.manual_override (T005)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from coordinare_service_inference.manual_override import (
    OVERRIDE_PATH_ENV_VAR,
    OVERRIDE_RELATIVE_PATH,
    apply_manual_override,
)
from coordinare_service_inference.schema import ServicesManifest


def _write_override(project_root: Path, payload: dict | str) -> Path:
    target = project_root / OVERRIDE_RELATIVE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return target


def _valid_redis_payload() -> dict:
    return {
        "services": [
            {
                "name": "redis",
                "binary": "redis-server",
                "version": "7.2",
                "data_dir": "/tmp/redis-data",
                "port": 6379,
                "why_needed": "Manual override for redis",
                "sources": [".coordinare/score.json"],
            },
        ],
        "cache_inputs": [".coordinare/score.json"],
        # agent_version intentionally omitted to test default
    }


def test_no_override_file_returns_not_applied(tmp_path):
    result = apply_manual_override(
        project_root=tmp_path, output_root=tmp_path / "out",
    )

    assert result.applied is False
    assert "no override file" in result.reason
    assert result.manifest is None
    assert result.scripts_dir is None
    # Output dir must not be created when override absent.
    assert not (tmp_path / "out").exists()


def test_invalid_json_returns_not_applied(tmp_path):
    _write_override(tmp_path, "{ not json")

    result = apply_manual_override(
        project_root=tmp_path, output_root=tmp_path / "out",
    )

    assert result.applied is False
    assert "not valid JSON" in result.reason


def test_schema_violation_returns_not_applied(tmp_path):
    bad = {
        "services": [
            {
                # missing required fields like binary, port, etc.
                "name": "redis",
            },
        ],
        "cache_inputs": [],
    }
    _write_override(tmp_path, bad)

    result = apply_manual_override(
        project_root=tmp_path, output_root=tmp_path / "out",
    )

    assert result.applied is False
    assert "ServicesManifest schema" in result.reason


def test_happy_path_drops_artifacts_into_services_subdir(tmp_path):
    _write_override(tmp_path, _valid_redis_payload())
    out = tmp_path / "devenv-redis-abc"

    result = apply_manual_override(project_root=tmp_path, output_root=out)

    assert result.applied is True
    assert result.scripts_dir == out / "services"
    assert (out / "services" / "services.json").is_file()
    for script_name in ("services-start.sh", "services-stop.sh", "services-health.sh"):
        path = out / "services" / script_name
        assert path.is_file()
        # Executable bit must be set; the runtime invokes them directly.
        mode = os.stat(path).st_mode
        assert mode & stat.S_IXUSR
        assert path.read_text().startswith("#!/usr/bin/env bash")

    manifest_on_disk = ServicesManifest.model_validate_json(
        (out / "services" / "services.json").read_text(),
    )
    assert [s.name for s in manifest_on_disk.services] == ["redis"]
    # Default agent_version "manual-override" must be applied when omitted.
    assert manifest_on_disk.agent_version == "manual-override"


def test_explicit_agent_version_in_override_is_overwritten(tmp_path):
    # Operator-supplied agent_version is forced to "manual-override" so
    # downstream telemetry and the cache-key composition can always
    # distinguish hand-authored manifests from LLM-produced ones.
    payload = _valid_redis_payload()
    payload["agent_version"] = "operator-handwritten-v3"
    _write_override(tmp_path, payload)

    result = apply_manual_override(
        project_root=tmp_path, output_root=tmp_path / "out",
    )

    assert result.applied is True
    assert result.manifest.agent_version == "manual-override"


def test_validation_failure_aborts_artifact_drop(tmp_path, monkeypatch):
    _write_override(tmp_path, _valid_redis_payload())

    # Force validate() to return a failure result.
    from coordinare_service_inference import manual_override as mod
    from coordinare_service_inference.validator import ValidationResult

    fake_result = ValidationResult(
        phase="health", stdout="", stderr="port not bound", ok=False, returncode=1,
    )
    monkeypatch.setattr(mod, "validate", lambda scripts: fake_result)

    out = tmp_path / "out"
    result = apply_manual_override(
        project_root=tmp_path, output_root=out, run_validation=True,
    )

    assert result.applied is False
    assert "failed dry-run" in result.reason
    assert result.validation is fake_result
    # No artifacts must be written on validation failure.
    assert not (out / "services").exists()


def test_validation_success_writes_artifacts(tmp_path, monkeypatch):
    _write_override(tmp_path, _valid_redis_payload())

    from coordinare_service_inference import manual_override as mod
    from coordinare_service_inference.validator import ValidationResult

    monkeypatch.setattr(
        mod,
        "validate",
        lambda scripts: ValidationResult(
            phase=None, stdout="all good", stderr="", ok=True, returncode=0,
        ),
    )

    out = tmp_path / "out"
    result = apply_manual_override(
        project_root=tmp_path, output_root=out, run_validation=True,
    )

    assert result.applied is True
    assert result.validation is not None and result.validation.ok
    assert (out / "services" / "services-start.sh").is_file()


def test_explicit_override_path_argument(tmp_path):
    custom = tmp_path / "elsewhere" / "manifest.json"
    custom.parent.mkdir(parents=True)
    custom.write_text(json.dumps(_valid_redis_payload()))
    out = tmp_path / "out"

    result = apply_manual_override(
        project_root=tmp_path, output_root=out, override_path=custom,
    )

    assert result.applied is True
    assert (out / "services" / "services.json").is_file()


def test_env_var_override_path(tmp_path, monkeypatch):
    custom = tmp_path / "via-env.json"
    custom.write_text(json.dumps(_valid_redis_payload()))
    monkeypatch.setenv(OVERRIDE_PATH_ENV_VAR, str(custom))
    out = tmp_path / "out"

    result = apply_manual_override(project_root=tmp_path, output_root=out)

    assert result.applied is True
    assert (out / "services" / "services.json").is_file()


def test_explicit_arg_beats_env_var(tmp_path, monkeypatch):
    # Env var points at a bogus path; the explicit kwarg must win.
    monkeypatch.setenv(OVERRIDE_PATH_ENV_VAR, str(tmp_path / "does-not-exist.json"))
    explicit = tmp_path / "explicit.json"
    explicit.write_text(json.dumps(_valid_redis_payload()))
    out = tmp_path / "out"

    result = apply_manual_override(
        project_root=tmp_path, output_root=out, override_path=explicit,
    )

    assert result.applied is True


def test_cache_manifest_sidecar_written(tmp_path):
    payload = _valid_redis_payload()
    payload["cache_inputs"] = ["Gemfile", "config/database.yml", ".coordinare/score.json"]
    _write_override(tmp_path, payload)
    out = tmp_path / "out"

    result = apply_manual_override(project_root=tmp_path, output_root=out)

    assert result.applied is True
    sidecar = out / "services" / "cache_manifest.txt"
    assert sidecar.is_file()
    assert sidecar.read_text().splitlines() == [
        "Gemfile",
        "config/database.yml",
        ".coordinare/score.json",
    ]
