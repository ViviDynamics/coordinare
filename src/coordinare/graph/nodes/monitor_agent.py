"""Legacy monitor_agent node — thin wrapper delegating to monitor_performer (019).

Deprecated: This module is retained for backward compatibility during the
migration period.  All new code should import monitor_performer directly.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from coordinare.graph.nodes.monitor_performer import monitor_performer

if TYPE_CHECKING:
    from coordinare.graph.state import CoordinareState

logger = structlog.get_logger(__name__)

_DEPRECATION_LOGGED = False


async def monitor_agent(state: CoordinareState) -> CoordinareState:
    """Delegate to monitor_performer.  Logs a deprecation warning once."""
    global _DEPRECATION_LOGGED
    if not _DEPRECATION_LOGGED:
        logger.warning(
            "monitor_agent.deprecated",
            msg="monitor_agent is deprecated — use monitor_performer instead",
        )
        _DEPRECATION_LOGGED = True
    return await monitor_performer(state)
