"""Junie backend adapter.

Junie is integrated as an OpenCode-compatible ``serve`` backend.
"""
from __future__ import annotations

import os

from performer.backends.opencode import OpenCodeAdapter


class JunieBackend(OpenCodeAdapter):
    """Adapter for the ``junie`` CLI using the OpenCode HTTP protocol."""

    def __init__(self) -> None:
        executable = os.environ.get("JUNIE_EXECUTABLE", "junie")
        super().__init__(executable=executable, adapter_name="junie")
