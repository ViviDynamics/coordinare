"""080 — the think_once error-marker regex is duplicated across two deployable
units (coordinare config + performer proxy). Pin them byte-identical so they
can't drift (same rationale as the 077 attribution-header sync test).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from coordinare.config import DEFAULT_THINK_ONCE_ERROR_PATTERN

_PERFORMER_SRC = Path(__file__).resolve().parents[1] / "agent" / "performer" / "src"
if str(_PERFORMER_SRC) not in sys.path:
    sys.path.insert(0, str(_PERFORMER_SRC))

try:
    from performer.proxy.strategies import DEFAULT_ERROR_PATTERN as _PERFORMER_PATTERN
except Exception:  # pragma: no cover - performer source not present
    _PERFORMER_PATTERN = None


@pytest.mark.skipif(_PERFORMER_PATTERN is None, reason="performer source not importable")
def test_error_pattern_matches_across_units():
    assert DEFAULT_THINK_ONCE_ERROR_PATTERN == _PERFORMER_PATTERN
