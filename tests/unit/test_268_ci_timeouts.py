"""A timed out test must exit even when cleanup and a non daemon thread hang."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_TIMEOUT_SECONDS = 1
PROCESS_DEADLINE_SECONDS = 10
E2E_TIMEOUT_METHOD = "thread"

HANGING_TEST = """\
from __future__ import annotations

import threading

import pytest

HANG_DURING_CALL = {hang_during_call}


@pytest.fixture
def stuck_cleanup():
    yield
    threading.Event().wait()


def test_hang(stuck_cleanup):
    threading.Thread(target=threading.Event().wait, daemon=False).start()
    if HANG_DURING_CALL:
        threading.Event().wait()
"""


@pytest.mark.parametrize("hang_during_call", [True, False], ids=["call", "teardown"])
def test_pytest_timeout_exits_despite_stuck_cleanup(
    tmp_path: Path, hang_during_call: bool,
) -> None:
    """Use the E2E job's timeout method, with a shorter per test deadline."""
    test_file = tmp_path / "test_hang.py"
    test_file.write_text(
        textwrap.dedent(HANGING_TEST).format(hang_during_call=hang_during_call),
        encoding="utf-8",
    )
    child_env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("PYTEST_")
    }
    child_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    started = time.monotonic()
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "-p", "pytest_timeout",
            "-c", str(PROJECT_ROOT / "pyproject.toml"),
            f"--confcutdir={tmp_path}", f"--timeout={TEST_TIMEOUT_SECONDS}",
            "-o", "addopts=", "-o", f"timeout_method={E2E_TIMEOUT_METHOD}", str(test_file),
        ],
        cwd=tmp_path,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=PROCESS_DEADLINE_SECONDS,
        check=False,
    )
    assert result.returncode == pytest.ExitCode.TESTS_FAILED, result.stdout + result.stderr
    assert "Timeout" in result.stdout, result.stdout + result.stderr
    assert time.monotonic() - started < PROCESS_DEADLINE_SECONDS


def test_the_e2e_job_ends_the_process_on_a_timeout() -> None:
    """Browser teardown can hang after a signal timeout, so the E2E job uses the thread method.

    The unit and integration jobs keep the default signal method, so one slow test fails alone
    instead of ending the whole run.
    """
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "pr-ci.yml").read_text(encoding="utf-8")
    # 289: the browser files arrive through scripts/ci_shard.py, so match the browser flag.
    e2e_runs = [line for line in workflow.splitlines() if "pytest" in line and "--browser" in line]
    assert e2e_runs
    assert all(f"timeout_method={E2E_TIMEOUT_METHOD}" in line for line in e2e_runs)
