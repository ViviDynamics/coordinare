"""Execute the build driver with tool doubles to check its orchestration contract."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def build_sandbox(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    scripts = tmp_path / "bin"
    scripts.mkdir()
    shutil.copy2(ROOT / "bin/build", scripts / "build")
    log = tmp_path / "calls.jsonl"
    tool = scripts / "uv"
    tool.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "with open(os.environ['BUILD_TEST_LOG'], 'a') as log:\n"
        "    log.write(json.dumps({'tool': Path(sys.argv[0]).name, 'args': sys.argv[1:],\n"
        "        'inference': os.environ.get('COORDINARE_INFERENCE_MAX_TOKENS'),\n"
        "        'hermes': os.environ.get('HERMES_CONTEXT_WINDOW')}) + '\\n')\n"
        "if Path(sys.argv[0]).name == 'docker' and sys.argv[1] == 'inspect':\n"
        "    print('100000000')\n"
    )
    tool.chmod(0o755)
    shutil.copy2(tool, scripts / "docker")
    env = dict(os.environ, PATH=f"{scripts}{os.pathsep}{os.environ['PATH']}",
               BUILD_TEST_LOG=str(log), COORDINARE_INFERENCE_MAX_TOKENS="1",
               HERMES_CONTEXT_WINDOW="1")
    return scripts / "build", env


def _calls(env: dict[str, str]) -> list[dict]:
    return [json.loads(line) for line in Path(env["BUILD_TEST_LOG"]).read_text().splitlines()]


def test_packages_installed_before_separate_full_suites(build_sandbox) -> None:
    script, env = build_sandbox
    result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = _calls(env)
    assert calls[0]["args"] == ["sync", "--extra", "dev"]
    assert calls[1]["args"] == ["pip", "install", "-e", "agent/performer[dev]"]
    pytest_calls = [call for call in calls if "pytest" in call["args"]]
    assert len(pytest_calls) == 3
    for call, tree in zip(pytest_calls, ["tests/", "tests/", "agent/performer/tests/"], strict=True):
        assert call["args"][:4] == ["run", "--no-sync", "pytest", tree]
        assert call["inference"] is None and call["hermes"] is None
    assert "--cov-fail-under=90" in pytest_calls[1]["args"]


def test_docker_size_failure_reaches_remaining_checks_and_summary(build_sandbox) -> None:
    script, env = build_sandbox
    result = subprocess.run(["bash", str(script), "--all"], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert "SC-007 FAIL" in result.stderr
    assert "Failed:" in result.stdout
    assert "  - Docker: image size gap (SC-007)" in result.stdout
    calls = _calls(env)
    assert any("coordinare-performer:extra" in call["args"] and "run" in call["args"] for call in calls)
    browser = next(call for call in calls if "tests/e2e/" in call["args"])
    assert browser["inference"] is None and browser["hermes"] is None


def test_fraction_below_coverage_floor_fails_the_process(tmp_path: Path) -> None:
    # 899 / 1000 statements = 89.90%: precision=0 incorrectly rounds this up to
    # a passing 90 even though pytest-cov prints a failure in its final summary.
    sample = "\n".join(f"value_{n} = {n}" for n in range(898))
    sample += "\ndef uncovered():\n" + "\n".join(f"    value_{n} = {n}" for n in range(101))
    (tmp_path / "coverage_boundary.py").write_text(sample + "\n")
    test = tmp_path / "test_boundary.py"
    test.write_text("import coverage_boundary\ndef test_import():\n    assert coverage_boundary.value_0 == 0\n")
    env = dict(os.environ, PYTHONPATH=str(tmp_path), COVERAGE_FILE=str(tmp_path / ".coverage"))
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(ROOT / "pyproject.toml"),
         str(test), "--confcutdir", str(tmp_path), "--cov=coverage_boundary",
         f"--cov-config={ROOT / 'pyproject.toml'}",
         "--cov-report=term", "--cov-fail-under=90", "-q"],
        env=env, cwd=tmp_path, capture_output=True, text=True,
    )
    assert "Total coverage: 89.90%" in result.stdout
    assert result.returncode == 1, result.stdout + result.stderr
