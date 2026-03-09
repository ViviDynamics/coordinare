"""Shared pytest fixtures for the performer test suite."""
from __future__ import annotations

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
