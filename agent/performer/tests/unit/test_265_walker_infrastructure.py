"""Unexpected walker failures must be reported as infrastructure."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from coordinare_service_inference.cache_provenance import RESULT_SENTINEL

from performer import cache_verifier

WALK_MAX_ENTRIES = 100
WALK_MAX_BYTES = 1024
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700


def test_unexpected_walker_exception_reports_transient(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("DEVENV", "/devenv/sym")
    monkeypatch.setenv("PROVENANCE_CONFIG", json.dumps({"activate_sh": "", "max_entries": WALK_MAX_ENTRIES, "max_bytes": WALK_MAX_BYTES}))

    def broken_walk(*args: Any, **kwargs: Any) -> None:
        raise RecursionError("injected walker failure")

    monkeypatch.setattr(cache_verifier, "walk_summary", broken_walk)
    cache_verifier.main()
    output = capsys.readouterr().out
    assert output.startswith(RESULT_SENTINEL)
    assert json.loads(output.removeprefix(RESULT_SENTINEL)) == {"error": "RecursionError", "transient": True}


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_claim_permission_failure_is_an_actionable_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str,
) -> None:
    cache = tmp_path / "claim"
    cache.mkdir()
    (cache / "activate.sh").write_text("")
    denied = cache / "private-entry"
    if kind == "file":
        denied.write_text("tool")
        denied.chmod(PRIVATE_FILE_MODE)
    else:
        denied.mkdir(mode=PRIVATE_DIRECTORY_MODE)
        (denied / "child").write_text("tool")
    monkeypatch.setenv("DEVENV", str(cache))
    monkeypatch.setenv("PROVENANCE_CONFIG", json.dumps({
        "activate_sh": "", "max_entries": WALK_MAX_ENTRIES, "max_bytes": WALK_MAX_BYTES,
    }))
    original_open = os.open

    def unprivileged_open(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        # Emulate uid 10001 opening root owned entries. The test runner owns its
        # temp files, so its own uid would otherwise be allowed through 0600/0700.
        if Path(name).name == denied.name:
            raise PermissionError(str(denied))
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", unprivileged_open)
    cache_verifier.main()
    result = json.loads(capsys.readouterr().out.removeprefix(RESULT_SENTINEL))
    assert result["transient"] is False
    for instruction in ("world-readable", "world-traversable", "o+rX", "10001", "fsGroup"):
        assert instruction in result["error"]
