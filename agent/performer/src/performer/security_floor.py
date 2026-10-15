from __future__ import annotations

import asyncio
import io
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from performer.security_scanner import ScannerError, scan_diff

if TYPE_CHECKING:
    from performer.models import Performance, Score, Stand
    from performer.protocol import PerformerResponse

logger = structlog.get_logger(__name__)
SCAN_VERSION = 1
SCAN_TOOLS = ("semgrep", "bandit")
_GIT = shutil.which("git") or "/usr/bin/git"
_GIT_TIMEOUT_SECONDS = 120
_REGULAR_MODES = {"100644", "100755"}
_SCAN_CONTROL_FILES = {".semgrepignore", ".gitignore", ".bandit"}


def _fail(reason: str) -> None:
    raise ScannerError(reason)


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        [_GIT, *args], cwd=root, check=True, capture_output=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    ).stdout


def _scan_head(request: Any, root: Path) -> tuple[list[dict], bool]:
    files = _scan_files(request)
    head = _git(root, "rev-parse", "HEAD").decode().strip()
    if request.get("head_sha") != head or not head:
        _fail("security scan head changed or unavailable")
    entries = {}
    for entry in _git(root, "ls-tree", "-r", "-z", head).split(b"\0"):
        if entry:
            metadata, name = entry.split(b"\t", 1)
            entries[name.decode()] = metadata.decode().split()[0]
    targets = _head_targets(files, entries)
    if not targets:
        return [], False
    return _scan_export(root, head, targets), True


def _head_targets(files: list[str], entries: dict[str, str]) -> list[str]:
    targets = []
    for name in files:
        if name not in entries:
            if any(path.startswith(f"{name}/") for path in entries):
                _fail("scan target is a directory")
            continue
        if entries[name] not in _REGULAR_MODES:
            _fail("scan target is not a regular file")
        if Path(name).name not in _SCAN_CONTROL_FILES:
            targets.append(name)
    return targets


def _scan_export(root: Path, head: str, targets: list[str]) -> list[dict]:
    # Never extract archive paths, links or repository scanner configuration.
    # Compare against ls-tree so export-ignore cannot silently remove a target.
    with tempfile.TemporaryDirectory(prefix="coordinare-security-", dir="/tmp") as directory:
        export = Path(directory).resolve()
        if export.is_relative_to(root):
            _fail("security export must be outside workspace")
        remaining = set(targets)
        archive_bytes = _git(root, "archive", "--format=tar", head)
        with tarfile.open(fileobj=io.BytesIO(archive_bytes)) as archive:
            for member in archive:
                if member.name not in remaining:
                    continue
                if not member.isfile():
                    _fail("archive target is not a regular file")
                source = archive.extractfile(member)
                if source is None:
                    _fail("archive target unavailable")
                target = export / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
                remaining.remove(member.name)
        if remaining:
            _fail("head files missing from security export")
        return scan_diff(targets, export)


async def run_security_floor(score: Score, stand: Stand) -> dict[str, Any]:
    """Run the mandatory prose security scan once, outside the model's control."""
    report: dict[str, Any] = {
        "version": SCAN_VERSION, "complete": False, "tools": list(SCAN_TOOLS), "findings": [],
    }
    try:
        findings, scanned = await asyncio.to_thread(_scan_head, score.security_floor, stand.path.resolve())
        report["findings"] = findings
        report["complete"] = True
        if not scanned:
            report["reason"] = "no files to scan"
    except Exception as exc:
        # Only code-owned summaries cross the wire, never raw scanner output.
        report["reason"] = "scan request or tool failed"
        logger.warning("security_floor.unavailable", error_type=type(exc).__name__)
    return report


def _scan_files(request: Any) -> list[str]:
    if not isinstance(request, dict) or type(request.get("version")) is not int or request["version"] != SCAN_VERSION:
        _fail("unsupported or missing security scan request")
    if request.get("diff_status") not in ("ok", "empty"):
        _fail("PR diff unavailable or changed paths overflow")
    files = request.get("files")
    if not isinstance(files, list) or any(not isinstance(f, str) or not f for f in files):
        _fail("changed paths unavailable")
    if not isinstance(request.get("head_sha"), str) or not request["head_sha"]:
        _fail("security scan head unavailable")
    for name in files:
        target = Path(name)
        if target.is_absolute() or not target.parts or ".." in target.parts or "\0" in name or str(target) != name:
            _fail("unsafe scan path")
    return files


def attach_security_floor(perf: Performance, response: PerformerResponse) -> PerformerResponse:
    """Preserve runtime evidence on initial and repeated terminal responses."""
    if perf.role != "security" or perf.score.workflow == "security":
        return response
    return response.model_copy(update={
        "report": {**(response.report or {}), "security_floor": perf.security_floor_report},
    })
