"""Runtime source identity follows portable Git modes across host filesystems."""
from __future__ import annotations

import hashlib
from pathlib import Path


def test_executable_bit_and_link_target_are_preserved_without_host_symlink_permissions(tmp_path: Path) -> None:
    from scripts.private_runtime_probe_source import inventory

    executable = tmp_path / "run"
    executable.write_bytes(b"payload\x00\xff")
    executable.chmod(0o755)
    (tmp_path / "alias").symlink_to("run")
    assert inventory(tmp_path) == {
        "alias": {"mode": "120000", "link": "run"},
        "run": {"mode": "100755", "sha256": hashlib.sha256(b"payload\x00\xff").hexdigest(), "bytes": 9},
    }
    executable.chmod(0o644)
    assert inventory(tmp_path)["run"]["mode"] == "100644"
