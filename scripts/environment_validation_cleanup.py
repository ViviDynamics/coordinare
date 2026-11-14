"""Remove disposable validation contents, including owner-read-only directories."""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def remove_contents(root: Path) -> None:
    """Restore directory owner permissions inside the owned root without following links."""
    root.chmod(root.stat().st_mode | 0o700)
    for parent, directories, _ in os.walk(root, followlinks=False):
        for name in directories:
            directory = Path(parent) / name
            if not directory.is_symlink():
                directory.chmod(directory.stat().st_mode | 0o700, follow_symlinks=False)
    for child in root.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
