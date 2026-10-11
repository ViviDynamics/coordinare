"""Structured audit logging for mutating MCP tool calls (spec US6)."""

from __future__ import annotations

from typing import Literal

import structlog

log = structlog.get_logger(__name__)


def emit_tool_invoked(
    *,
    tool: str,
    action: str | None,
    outcome: Literal["ok", "error"],
    credential_fingerprint: str,
    duration_ms: int,
    error_kind: str | None = None,
) -> None:
    """Emit the one ``mcp.tool_invoked`` line per mutating call (FR6/US6)."""
    log.info(
        "mcp.tool_invoked",
        tool=tool,
        action=action,
        outcome=outcome,
        error_kind=error_kind,
        credential_fingerprint=credential_fingerprint,
        duration_ms=duration_ms,
    )


__all__ = ["emit_tool_invoked"]
