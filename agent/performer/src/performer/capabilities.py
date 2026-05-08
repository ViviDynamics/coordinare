"""Capability probe for the performer container (spec 056, T021).

At server startup we sniff the runtime environment to populate the
``PerformerCapabilities`` payload exposed via ``GET /status``. This is the
authoritative answer the coordinare uses to decide which roles a performer
can take. It is intentionally conservative: we only advertise what we can
verify on PATH or by import.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

from performer.server.models import PerformerCapabilities

_BACKEND_BINARIES: dict[str, str] = {
    "claude_code": "claude",
    "codex": "codex",
    "cursor": "cursor-agent",  # cursor install.sh creates ~/.local/bin/cursor-agent
    "junie": "junie",
    "opencode": "opencode",
}

_TOOL_BINARIES: dict[str, str] = {
    "git": "git",
    "node": "node",
    "python": "python3",
    "jq": "jq",
    "ripgrep": "rg",
    "shell": "bash",
}


@dataclass(frozen=True)
class CapabilityProbeResult:
    backends: list[str]
    tool_flags: list[str]


def _has_binary(name: str) -> bool:
    return shutil.which(name) is not None


def _any_binary(*names: str) -> bool:
    return any(shutil.which(n) is not None for n in names)


def _playwright_installed() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


def probe_capabilities() -> PerformerCapabilities:
    backends = [name for name, binary in _BACKEND_BINARIES.items() if _has_binary(binary)]
    tool_flags = [flag for flag, binary in _TOOL_BINARIES.items() if _has_binary(binary)]
    if _playwright_installed():
        tool_flags.append("browser")
    if _any_binary("ruff", "pylint", "flake8", "mypy"):
        tool_flags.append("lint")
    if _any_binary("black", "ruff", "isort", "prettier"):
        tool_flags.append("format")
    if _any_binary("pytest", "jest", "mocha"):
        tool_flags.append("test_runner")
    return PerformerCapabilities(backends=backends, tool_flags=sorted(set(tool_flags)))


__all__ = ["CapabilityProbeResult", "probe_capabilities"]
