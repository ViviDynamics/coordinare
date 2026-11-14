"""A dropped obligation is correction feedback (Dettmore e2e-84, e2e-89): the candidate is never accepted, and the
author is told exactly what to restore."""
from __future__ import annotations

from typing import Any


def told_to_restore(calls: list[list[dict[str, Any]]]) -> bool:
    """Some later model call carried obligation_removed feedback."""
    return any("obligation_removed" in str(message.get("content", "")) for call in calls for message in call)
