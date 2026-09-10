"""Re-export of the shared CI-detection package.

339: the detection logic moved to ``packages/ci_detection`` so the performer
can use it too. The performer image installs that package but has never
shipped ``coordinare`` (``Dockerfile.base`` copies ``packages/`` and
``agent/performer`` only), so every in-container
``from coordinare.services.ci_detection import detect`` raised ImportError and
the implementer hard-blocked its cards with "no test runner detected".

This module stays as the coordinare-side import path so existing call sites and
tests keep working.
"""
from __future__ import annotations

from coordinare_ci_detection import (
    CIDetectionResult,
    _verify_tool,
    detect,
)

__all__ = ["CIDetectionResult", "_verify_tool", "detect"]
