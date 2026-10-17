"""Image publication happens before pytest starts its per-test deadline."""
from __future__ import annotations

import contextlib
import subprocess
import sys

import pytest

from tests.integration import reviewed_environment


@pytest.mark.parametrize("exit_code", [0, 1, 5])
def test_launcher_prepares_once_forwards_every_test_and_preserves_failure(monkeypatch, exit_code):
    image = "trusted/performer@sha256:" + "a" * 64
    events = []

    @contextlib.contextmanager
    def prepare():
        events.append("prepare")
        yield image
        events.append("cleanup")

    def execute(argv, *, env, check, timeout):
        assert events == ["prepare"]
        assert argv == [sys.executable, "-m", "pytest", "first.py", "second.py", "-v"]
        assert env["COORDINARE_E2E_PERFORMER_IMAGE"] == image
        assert timeout == 300
        assert check is False
        events.append("pytest")
        return subprocess.CompletedProcess(argv, exit_code)

    monkeypatch.setattr(reviewed_environment, "prepared_performer_image", prepare, raising=False)
    monkeypatch.setattr(reviewed_environment.subprocess, "run", execute)
    assert reviewed_environment.run_image_tests(["first.py", "second.py", "-v"]) == exit_code
    assert events == ["prepare", "pytest", "cleanup"]


def test_launcher_cleans_owned_image_when_test_process_times_out(monkeypatch):
    events = []

    @contextlib.contextmanager
    def prepare():
        try:
            yield "trusted/performer@sha256:" + "a" * 64
        finally:
            events.append("cleanup")

    def execute(*args, **kwargs):
        raise subprocess.TimeoutExpired("pytest", 300)

    monkeypatch.setattr(reviewed_environment, "prepared_performer_image", prepare)
    monkeypatch.setattr(reviewed_environment.subprocess, "run", execute)
    with pytest.raises(subprocess.TimeoutExpired):
        reviewed_environment.run_image_tests(["first.py"])
    assert events == ["cleanup"]


def test_preparation_preserves_explicit_digest_when_image_has_multiple_aliases(monkeypatch):
    import json

    pinned = "127.0.0.1:1234/prepared@sha256:" + "a" * 64
    monkeypatch.setenv("COORDINARE_E2E_PERFORMER_IMAGE", pinned)
    monkeypatch.setattr(reviewed_environment, "_docker", lambda *args: json.dumps([
        {"RepoDigests": ["other/source@sha256:" + "b" * 64, pinned]},
    ]))
    with reviewed_environment.prepared_performer_image() as actual:
        assert actual == pinned
