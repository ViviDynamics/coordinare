"""Backend factory — maps AGENT_BACKEND names to adapter classes."""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from performer.backends.base import BackendAdapter


class UnsupportedBackendError(ValueError):
    """Raised when an unknown AGENT_BACKEND value is requested."""


def get_backend(name: str) -> "BackendAdapter":
    """Return a new BackendAdapter instance for *name*.

    Raises UnsupportedBackendError if *name* is not in SUPPORTED_BACKENDS.
    """
    # Import lazily to avoid circular imports at module load time.
    from performer.backends.opencode import OpenCodeAdapter

    SUPPORTED_BACKENDS: dict[str, type[BackendAdapter]] = {
        "opencode": OpenCodeAdapter,
    }

    cls = SUPPORTED_BACKENDS.get(name)
    if cls is None:
        supported = ", ".join(sorted(SUPPORTED_BACKENDS))
        raise UnsupportedBackendError(
            f"unsupported backend {name!r}; supported: {supported}"
        )
    return cls()
