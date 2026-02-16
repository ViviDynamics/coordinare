from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tests.integration.runtime.test_runtime_helpers import build_shell_command


def test_shell_script_exists_and_executable() -> None:
    script = Path("scripts/run-coordinare.sh")
    assert script.exists()
    assert os.access(script, os.X_OK)


def test_shell_runtime_command_shape(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text("project_name: test\n")

    command = build_shell_command(config_file, structured=True, log_level="debug")

    assert command[0] == "scripts/run-coordinare.sh"
    assert "--structured-output" in command
    assert "--log-level" in command


def test_shell_help_smoke() -> None:
    result = subprocess.run(
        ["scripts/run-coordinare.sh", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--structured-output" in result.stdout
