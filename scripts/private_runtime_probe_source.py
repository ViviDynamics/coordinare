"""Portable fixture identity for the private-runtime diagnostic."""
from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


def inventory(root: Path) -> dict[str, dict[str, Any]]:
    """Record Git-significant modes, complete bytes and literal symlink targets."""
    entries: dict[str, dict[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            entries[relative] = {"mode": "120000", "link": str(path.readlink())}
        elif path.is_file():
            data = path.read_bytes()
            entries[relative] = {"mode": "100755" if path.stat().st_mode & 0o111 else "100644",
                                 "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        elif path.is_dir():
            entries[relative] = {"kind": "directory"}
        else:
            raise ValueError("unsupported fixture entry")
    return entries
