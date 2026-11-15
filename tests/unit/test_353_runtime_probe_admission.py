"""A diagnostic runtime probe must refuse unsafe inputs before starting resources."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_missing_resource_limits_hold_before_creating_runtime(tmp_path: Path) -> None:
    configuration = tmp_path / "config.json"
    configuration.write_text(json.dumps({"image": "sha256:" + "a" * 64}))
    output = tmp_path / "evidence"
    result = subprocess.run(
        [sys.executable, "-m", "scripts.probe_private_service_runtime",
         "--config", str(configuration), "--output", str(output)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 1, result.stderr
    report = json.loads((output / "result.json").read_text())
    assert report["status"] == "held"
    assert report["reason"] == "probe_configuration_invalid"
    assert report["resources_started"] is False
    assert "traceback" not in (result.stdout + result.stderr).lower()


def test_probe_refuses_to_overwrite_an_existing_attempt(tmp_path: Path) -> None:
    configuration = tmp_path / "config.json"
    configuration.write_text("{}")
    output = tmp_path / "evidence"
    output.mkdir()
    existing = output / "result.json"
    existing.write_text('{"status":"original-evidence"}\n')
    result = subprocess.run(
        [sys.executable, "-m", "scripts.probe_private_service_runtime",
         "--config", str(configuration), "--output", str(output)],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 1
    assert json.loads(existing.read_text()) == {"status": "original-evidence"}
