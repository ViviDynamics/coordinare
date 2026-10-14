"""Issue #229: an integration module's import-time cluster probe never aborts collection.

A kubectl or docker probe that hangs raised ``subprocess.TimeoutExpired`` while
pytest imported the module, and the whole Tests job died at collection. Each
probe must treat a failure to run the tool as "no cluster" and let the module
import, so its gate still applies and nothing else in the run is affected.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

PROBED_MODULES = [
    pytest.param("tests/integration/test_147_helm_install.py", "_cluster_available", id="147"),
    pytest.param("tests/integration/test_221_pod_churn_live.py", "_cluster_available", id="221"),
    pytest.param("tests/integration/test_225_egress_probe_live.py", "_cluster_available", id="225"),
    pytest.param(
        "tests/integration/test_148_docker_daemon_image.py",
        "_docker_available",
        id="148",
    ),
]

RUN_FAILURES = [
    pytest.param(lambda: subprocess.TimeoutExpired(cmd="kubectl", timeout=15), id="timeout"),
    pytest.param(lambda: FileNotFoundError(2, "No such file", "kubectl"), id="missing-binary"),
    pytest.param(lambda: OSError("exec format error"), id="oserror"),
]


def _import_with_run_failing(
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
    make_error: Callable[[], BaseException],
) -> ModuleType:
    def failing_run(*_args: object, **_kwargs: object) -> None:
        raise make_error()

    monkeypatch.setattr(subprocess, "run", failing_run)
    # Pretend every tool is installed, so the probe reaches the subprocess call.
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")

    path = REPO_ROOT / relative
    name = "probe_229_" + path.stem
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # the import itself is the assertion
    return module


@pytest.mark.parametrize("make_error", RUN_FAILURES)
@pytest.mark.parametrize(("relative", "probe"), PROBED_MODULES)
def test_import_succeeds_and_reports_no_cluster(
    monkeypatch: pytest.MonkeyPatch,
    relative: str,
    probe: str,
    make_error: Callable[[], BaseException],
) -> None:
    module = _import_with_run_failing(monkeypatch, relative, make_error)

    assert getattr(module, probe)() is False

    # The existing gate is still in place: no cluster means the module's tests
    # are not run.
    marks = module.pytestmark if isinstance(module.pytestmark, list) else [module.pytestmark]
    gates = [m for m in marks if m.name == "skipif"]
    assert gates, f"{relative} lost its cluster gate"
    assert gates[0].args[0] is True
