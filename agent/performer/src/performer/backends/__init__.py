"""Backend factory — maps AGENT_BACKEND names to adapter classes."""
from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from performer.backends.base import BackendAdapter


class UnsupportedBackendError(ValueError):
    """Raised when an unknown AGENT_BACKEND value is requested."""


def get_backend(name: str) -> "BackendAdapter":
    """Return a new BackendAdapter instance for *name*.

    Raises UnsupportedBackendError if *name* is not in SUPPORTED_BACKENDS.
    """
    # Import only the requested backend class on demand so optional
    # backend dependencies do not break unrelated configurations/tests.
    supported_backends: dict[str, tuple[str, str]] = {
        "opencode": ("performer.backends.opencode", "OpenCodeAdapter"),
        "junie": ("performer.backends.junie", "JunieBackend"),
        "cursor": ("performer.backends.cursor", "CursorBackend"),
        "claude_code": ("performer.backends.claude_code", "ClaudeCodeBackend"),
        "codex": ("performer.backends.codex", "CodexBackend"),
    }

    target = supported_backends.get(name)
    if target is None:
        supported = ", ".join(sorted(supported_backends))
        raise UnsupportedBackendError(
            f"unsupported backend {name!r}; supported: {supported}"
        )
    supported = ", ".join(sorted(supported_backends))
    module_name, class_name = target
    try:
        module = import_module(module_name)
        cls = getattr(module, class_name, None)
    except ImportError as exc:
        raise UnsupportedBackendError(
            f"backend {name!r} could not be loaded from module {module_name!r}; "
            f"supported: {supported}"
        ) from exc
    if cls is None:
        raise UnsupportedBackendError(
            f"backend {name!r} is misconfigured: class {class_name!r} "
            f"not found in module {module_name!r}; supported: {supported}"
        )
    return cls()
