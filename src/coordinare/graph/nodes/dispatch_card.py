"""Legacy dispatch_card node — thin wrapper delegating to dispatch_performer (019).

Deprecated: This module is retained for backward compatibility during the
migration period.  All new code should import dispatch_performer directly.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from coordinare.graph.nodes.dispatch_performer import dispatch_performer

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)

_DEPRECATION_LOGGED = False


async def dispatch_card(state: CoordinareState) -> CoordinareState:
    """Delegate to dispatch_performer.  Logs a deprecation warning once."""
    global _DEPRECATION_LOGGED
    if not _DEPRECATION_LOGGED:
        logger.warning(
            "dispatch_card.deprecated",
            msg="dispatch_card is deprecated — use dispatch_performer instead",
        )
        _DEPRECATION_LOGGED = True
    return await dispatch_performer(state)
