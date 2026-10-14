"""Router modules for the dashboard, one per route area (436)."""
from __future__ import annotations

from coordinare.dashboard.routers import (
    assistant,
    config,
    controls,
    pages,
    personas,
    run_logs,
    runs,
    state,
    symphonies,
)

__all__ = ["assistant", "config", "controls", "pages", "personas", "run_logs", "runs", "state", "symphonies"]
