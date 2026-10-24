"""358: the daemon image carries its release version, not the source tree's 0.1.0.

``Dockerfile.daemon`` installs from source with ``uv sync``, and the hatch version of an
editable install is read from ``src/coordinare/_version.py``. The image build stamps that
file from a ``COORDINARE_VERSION`` build arg before the install, with the same writer the
release wheels use, so the header, ``coordinare --version`` and the info metric agree.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "Dockerfile.daemon"
WORKFLOW = ROOT / ".github/workflows/main-branch-build.yml"
SCRIPT = ROOT / "scripts/release_packages.py"


def release_tools():
    spec = importlib.util.spec_from_file_location("release_packages", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source_tree(tmp_path: Path) -> Path:
    module = tmp_path / "src/coordinare/_version.py"
    module.parent.mkdir(parents=True)
    module.write_text('from __future__ import annotations\n\n__version__ = "0.1.0"\n', encoding="utf-8")
    return tmp_path


def test_stamping_writes_the_normalized_release_version(tmp_path: Path) -> None:
    root = _source_tree(tmp_path)
    assert release_tools().stamp_version_module(root, "2026.10.176") == "2026.10.176"
    namespace: dict[str, str] = {}
    exec((root / "src/coordinare/_version.py").read_text(encoding="utf-8"), namespace)
    assert namespace["__version__"] == "2026.10.176"


def test_stamping_normalizes_a_legacy_tag(tmp_path: Path) -> None:
    root = _source_tree(tmp_path)
    assert release_tools().stamp_version_module(root, "2026.09.01.3") == "2026.9.1.3"
    assert '__version__ = "2026.9.1.3"' in (root / "src/coordinare/_version.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("version", ["", "0.1.0", "latest", '2026.10.1"; import os'])
def test_stamping_refuses_anything_but_a_release_version(tmp_path: Path, version: str) -> None:
    root = _source_tree(tmp_path)
    with pytest.raises(ValueError, match="Invalid release version"):
        release_tools().stamp_version_module(root, version)
    assert '"0.1.0"' in (root / "src/coordinare/_version.py").read_text(encoding="utf-8")


def test_the_stamp_command_needs_no_repository(tmp_path: Path) -> None:
    root = _source_tree(tmp_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--version", "2026.10.176", "--stamp", str(root)],
        capture_output=True, text=True, check=False, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert '__version__ = "2026.10.176"' in (root / "src/coordinare/_version.py").read_text(encoding="utf-8")


def test_the_release_build_still_requires_a_repository() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--version", "2026.10.176"],
        capture_output=True, text=True, check=False, timeout=60,
    )
    assert result.returncode != 0
    assert "--repository" in result.stderr


def test_the_daemon_image_stamps_the_version_before_installing() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    arg = re.search(r"^ARG COORDINARE_VERSION\b", text, re.MULTILINE)
    stamp = re.search(r"^RUN .*release_packages\.py .*--version \"\$COORDINARE_VERSION\" --stamp /app", text, re.MULTILINE)
    install = re.search(r"^RUN\s+uv sync\b", text, re.MULTILINE)
    copy_src = re.search(r"^COPY src \./src$", text, re.MULTILINE)
    assert arg and stamp and install and copy_src
    assert copy_src.start() < stamp.start() < install.start()
    assert arg.start() < stamp.start()


def test_main_branch_build_passes_the_release_version_to_the_daemon_image() -> None:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    runs = "\n".join(step.get("run", "") for step in jobs["build-daemon"]["steps"])
    build = re.search(r"^\s*docker build .*-f Dockerfile\.daemon .*$", runs, re.MULTILINE)
    assert build is not None, runs
    assert '--build-arg "COORDINARE_VERSION=${VERSION}"' in build.group(0)
