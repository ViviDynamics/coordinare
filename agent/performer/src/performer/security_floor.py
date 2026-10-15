from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from performer.security_scanner import ScannerError, scan_diff

if TYPE_CHECKING:
    from pathlib import Path

    from performer.models import Performance, Score, Stand
    from performer.protocol import PerformerResponse

logger = structlog.get_logger(__name__)
SCAN_VERSION = 1
SCAN_TOOLS = ("semgrep", "bandit")


async def run_security_floor(score: Score, stand: Stand) -> dict[str, Any]:
    """Run the mandatory prose security scan once, outside the model's control."""
    report: dict[str, Any] = {
        "version": SCAN_VERSION, "complete": False, "tools": list(SCAN_TOOLS), "findings": [],
    }
    try:
        files = _scan_files(score.security_floor, stand.path.resolve())
        report["findings"] = await asyncio.to_thread(scan_diff, files, stand.path)
        report["complete"] = True
    except Exception as exc:
        # Only code-owned summaries cross the wire, never raw scanner output.
        report["reason"] = "scan request or tool failed"
        logger.warning("security_floor.unavailable", error_type=type(exc).__name__)
    return report


def _scan_files(request: Any, root: Path) -> list[str]:
    if not isinstance(request, dict) or type(request.get("version")) is not int or request["version"] != SCAN_VERSION:
        raise ScannerError("unsupported or missing security scan request")
    if request.get("diff_status") not in ("ok", "empty"):
        raise ScannerError("PR diff unavailable or changed paths overflow")
    files = request.get("files")
    if not isinstance(files, list) or any(not isinstance(f, str) or not f for f in files):
        raise ScannerError("changed paths unavailable")
    for name in files:
        target = root / name
        if name.startswith("/") or ".." in target.relative_to(root).parts:
            raise ScannerError("unsafe scan path")
        if not target.resolve().is_relative_to(root) or not target.is_file():
            raise ScannerError("scan target unavailable or outside workspace")
    return files


def attach_security_floor(perf: Performance, response: PerformerResponse) -> PerformerResponse:
    """Preserve runtime evidence on initial and repeated terminal responses."""
    if perf.role != "security" or perf.score.workflow == "security":
        return response
    return response.model_copy(update={
        "report": {**(response.report or {}), "security_floor": perf.security_floor_report},
    })
