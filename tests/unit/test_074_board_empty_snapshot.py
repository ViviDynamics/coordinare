"""The active-work board distinguishes an empty snapshot from a missing one."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from coordinare.dashboard import _DASHBOARD_JS_SOURCES


@pytest.mark.parametrize("case", ["held", "empty", "missing", "selected", "populated", "initial", "late", "idleMissing"])
def test_received_board_empty_state(tmp_path: Path, case: str) -> None:
    source = tmp_path / "performers.js"
    source.write_text(_DASHBOARD_JS_SOURCES["performers"])
    node = shutil.which("node")
    assert node is not None, "Node is required for dashboard checks"
    checks = Path(__file__).resolve().parents[1] / "js" / "board_empty_checks.js"
    result = subprocess.run([node, str(checks), str(source), case], capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
