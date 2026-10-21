"""Pinned inventory entries: path, byte size and git mode, taken from the immutable tree, never a read."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Mode = Literal['100644', '100755', '120000']
MODES: frozenset[str] = frozenset({'100644', '100755', '120000'})


@dataclass(frozen=True)
class InventoryEntry:
    path: str
    size: int
    mode: Mode
    target: str | None = None  # Recorded link text for mode 120000; never followed.
