"""Cursor backend adapter.

Cursor is integrated as an OpenCode-compatible ``serve`` backend.
"""
from __future__ import annotations

import os

from performer.backends.opencode import OpenCodeAdapter


class CursorBackend(OpenCodeAdapter):
    """Adapter for the ``cursor`` CLI using the OpenCode HTTP protocol."""

    def __init__(self) -> None:
        executable = os.environ.get("CURSOR_EXECUTABLE", "cursor")
        super().__init__(executable=executable, adapter_name="cursor")
