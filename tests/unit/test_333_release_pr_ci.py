"""Release packaging must be checked before merge, including the arm64 build."""

from __future__ import annotations

import shlex
from fnmatch import fnmatchcase
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _jobs() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/pr-ci.yml").read_text())["jobs"]


def test_release_filter_covers_packaging_and_its_own_smoke_sources() -> None:
    changes = _jobs()["release-changes"]
    assert changes["permissions"]["pull-requests"] == "read"
    assert changes["outputs"]["release"] == "${{ steps.paths.outputs.release }}"
    paths = next(step for step in changes["steps"] if step.get("id") == "paths")
    filters = yaml.safe_load(paths["with"]["filters"])["release"]
    assert {
        "pyproject.toml", "uv.lock", "agent/performer/pyproject.toml",
        "agent/performer/uv.lock", "Dockerfile.daemon", "agent/performer/Dockerfile.base",
        "agent/performer/Dockerfile.full", "registry", "registry-daemon", "bin/build",
        "Makefile", ".github/scripts/**", ".github/workflows/pr-ci.yml",
        "scripts/resolve_smoke_release.sh", "src/coordinare/services/env_manifest.py",
    } <= set(filters)
    assert "**" not in filters, "unrelated PRs should not build release images"


def test_release_lane_builds_arm64_and_exercises_release_selection() -> None:
    job = _jobs()["release-smoke"]
    assert job["needs"] == "release-changes"
    assert "needs.release-changes.outputs.release == 'true'" in job["if"]
    assert "!github.event.pull_request.draft" in job["if"]
    assert job["runs-on"] == "ubuntu-24.04-arm"
    assert job["timeout-minutes"] <= 20
    commands = "\n".join(step.get("run", "") for step in job["steps"])
    assert "docker build --platform linux/arm64" in commands
    assert "-f agent/performer/Dockerfile.base ." in commands
    assert "scripts/resolve_smoke_release.sh" in commands
    assert "tests/unit/test_env_manifest.py" in commands
    assert "tests/unit/test_197_review_fixes.py" in commands
    assert "tests/unit/test_197_rereview.py" in commands
    assert 'uv pip install -e "agent/performer[dev]"' in commands
    assert "docker push" not in commands
    assert job["permissions"] == {"contents": "read"}
    assert "release-smoke" not in _jobs()["build-success"]["needs"]


@pytest.mark.parametrize(("dockerfile", "context"), [
    ("agent/performer/Dockerfile.base", "."),
    ("agent/performer/Dockerfile.full", "agent/performer"),
])
def test_every_release_image_copy_input_triggers_the_release_lane(
    dockerfile: str, context: str,
) -> None:
    changes = _jobs()["release-changes"]
    paths = next(step for step in changes["steps"] if step.get("id") == "paths")
    filters = yaml.safe_load(paths["with"]["filters"])["release"]
    for line in (ROOT / dockerfile).read_text().splitlines():
        if not line.startswith("COPY "):
            continue
        for source in shlex.split(line)[1:-1]:
            # A directory COPY must also watch changes to files below it.
            probe = str(Path(context) / source)
            if source.endswith("/"):
                probe += "/build_input.py"
            assert any(fnmatchcase(probe, pattern) for pattern in filters), source
    assert {str(Path(context) / ".dockerignore"), dockerfile + ".dockerignore"} <= set(filters)
