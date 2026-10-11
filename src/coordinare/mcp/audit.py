"""Structured audit logging for mutating MCP tool calls (spec US6)."""

from __future__ import annotations

from typing import Any, Literal

import structlog

log = structlog.get_logger(__name__)


def emit_tool_invoked(
    *,
    tool: str,
    action: str | None,
    outcome: Literal["ok", "error"],
    **fields: Any,
) -> None:
    """Emit the one ``mcp.tool_invoked`` line per mutating call (FR6/US6).

    ``fields`` carries the remaining contract keys: ``error_kind`` (None when
    absent), ``credential_fingerprint`` and ``duration_ms``.
    """
    fields.setdefault("error_kind", None)
    log.info("mcp.tool_invoked", tool=tool, action=action, outcome=outcome, **fields)


__all__ = ["emit_tool_invoked"]
