"""Shared dependencies handed to every dashboard router (436)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class DashboardContext:
    daemon: Any
    store: Any
    metrics: Any
    health: Any
    config_path: Path | None
    assistant_on: bool
