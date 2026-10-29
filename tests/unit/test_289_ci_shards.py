"""289: CI shards together run exactly the tests one unsharded run would."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "ci_shard.py"
WORKFLOWS = (".github/workflows/pr-ci.yml", ".github/workflows/main-branch-build.yml")

_spec = importlib.util.spec_from_file_location("ci_shard", SCRIPT)
assert _spec is not None and _spec.loader is not None
ci_shard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ci_shard)


def _shards(root: str, count: int, *extra: str) -> list[list[str]]:
    return [
        subprocess.run(
            [sys.executable, str(SCRIPT), "--shard", str(n), "--of", str(count), *extra, root],
            cwd=PROJECT_ROOT, capture_output=True, text=True, check=True,
        ).stdout.split()
        for n in range(1, count + 1)
    ]


def _git_test_files(root: str) -> set[str]:
    listed = subprocess.run(
        ["git", "ls-files", root], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    return {path for path in listed if re.search(r"(^|/)(test_[^/]*|[^/]*_test)\.py$", path)}


def _workflow(rel: str) -> dict:
    return yaml.safe_load((PROJECT_ROOT / rel).read_text(encoding="utf-8"))


def test_every_test_file_lands_in_exactly_one_shard() -> None:
    """The unit lane skips tests/e2e and the browser lane runs it; together they cover every file."""
    unit = _shards("tests", 4, "--exclude", "tests/e2e")
    browser = _shards("tests/e2e", 3)
    flat = [path for shard in unit + browser for path in shard]
    assert len(flat) == len(set(flat)), "a file is in two shards and would run twice"
    assert set(flat) == _git_test_files("tests"), "a tracked test file is in no shard"
    assert all(unit) and all(browser), "an empty shard makes pytest exit 5"


def test_the_split_is_deterministic() -> None:
    assert _shards("tests/e2e", 3) == _shards("tests/e2e", 3)


def test_measured_durations_outweigh_the_test_count() -> None:
    files = [Path("a_test.py"), Path("b_test.py"), Path("c_test.py")]
    durations = {"a_test.py": 100.0, "b_test.py": 1.0, "c_test.py": 1.0}
    assert ci_shard.assign(files, 2, durations) == [[Path("a_test.py")], [Path("b_test.py"), Path("c_test.py")]]


def test_the_durations_file_names_real_files_when_present() -> None:
    durations = PROJECT_ROOT / "tests" / "ci-durations.json"
    if not durations.is_file():
        return
    stale = [path for path in json.loads(durations.read_text(encoding="utf-8")) if not (PROJECT_ROOT / path).is_file()]
    assert not stale, f"refresh tests/ci-durations.json; it names deleted files: {stale[:5]}"


@pytest.mark.parametrize("rel", WORKFLOWS)
def test_each_sharded_job_asks_for_as_many_shards_as_its_matrix_runs(rel: str) -> None:
    jobs = _workflow(rel)["jobs"]
    sharded = {name: job for name, job in jobs.items() if "ci_shard.py" in yaml.safe_dump(job, width=4096)}
    assert {"test", "coverage-shard"} <= set(sharded)
    for name, job in sharded.items():
        shards = job["strategy"]["matrix"]["shard"]
        assert shards == list(range(1, len(shards) + 1)), name
        assert f"--of {len(shards)}" in yaml.safe_dump(job, width=4096), name
        assert job["strategy"]["fail-fast"] is False, f"{name}: one red shard must not hide the others"


@pytest.mark.parametrize("rel", WORKFLOWS)
def test_the_coverage_floor_applies_to_the_combined_data(rel: str) -> None:
    jobs = _workflow(rel)["jobs"]
    shard_run = yaml.safe_dump(jobs["coverage-shard"], width=4096)
    assert "--cov-fail-under=0" in shard_run
    gate = jobs["coverage"]
    assert gate["needs"] == "coverage-shard"
    # A failed shard skips a plain dependent job, and a skipped job does not fail the gate.
    assert "always()" in gate["if"]
    steps = {step["name"]: step for step in gate["steps"]}
    assert "needs.coverage-shard.result != 'success'" in steps["Every shard succeeded"]["if"]
    assert "exit 1" in steps["Every shard succeeded"]["run"]
    combine = steps["Combine and apply the 90% floor"]["run"]
    assert "coverage combine" in combine
    assert "--fail-under=90" in combine


def test_the_browser_shards_cover_both_browsers() -> None:
    e2e = _workflow(".github/workflows/pr-ci.yml")["jobs"]["e2e"]
    assert e2e["strategy"]["matrix"]["browser"] == ["chromium", "firefox"]
    assert e2e["strategy"]["fail-fast"] is False
    assert "--of 3 tests/e2e" in yaml.safe_dump(e2e, width=4096)
    assert len(e2e["strategy"]["matrix"]["shard"]) == 3


@pytest.mark.parametrize("rel", WORKFLOWS)
def test_the_unit_shards_leave_tests_e2e_to_the_browser_lane(rel: str) -> None:
    for name in ("test", "coverage-shard"):
        assert "--of 4 tests --exclude tests/e2e" in yaml.safe_dump(_workflow(rel)["jobs"][name], width=4096), name


def test_tests_e2e_holds_only_browser_tests() -> None:
    """The unit lane skips tests/e2e on the strength of this.

    With the default ``-m 'not e2e'`` nothing there may be collected; an unmarked test would
    still run in the E2E lane, which ignores the marker, but would have been a unit test too.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "tests/e2e"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == pytest.ExitCode.NO_TESTS_COLLECTED, result.stdout + result.stderr


def test_duration_refresh_uses_medians_across_runs(tmp_path: Path) -> None:
    """Repeated runs are observations, while multiple reports within one run add."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    classname = "tests.unit.test_289_ci_shards"
    def report(path: Path, seconds: float) -> None:
        path.write_text(f'<testsuite><testcase classname="{classname}" time="{seconds}"/></testsuite>')
    report(first / "chromium.xml", 2)
    report(first / "firefox.xml", 4)
    report(second / "tests.xml", 10)
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts/ci_durations.py"), "--median", str(first), str(second)],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"tests/unit/test_289_ci_shards.py": 8.0}


def test_duration_refresh_does_not_treat_missing_observations_as_zero(tmp_path: Path) -> None:
    first = tmp_path / "first.xml"
    second = tmp_path / "second.xml"
    first.write_text('<testsuite><testcase classname="tests.unit.test_289_ci_shards" time="5"/></testsuite>')
    second.write_text('<testsuite><testcase classname="tests.unit.test_333_release_pr_ci" time="3"/></testsuite>')
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts/ci_durations.py"), "--median", str(first), str(second)],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "tests/unit/test_289_ci_shards.py": 5.0,
        "tests/unit/test_333_release_pr_ci.py": 3.0,
    }
