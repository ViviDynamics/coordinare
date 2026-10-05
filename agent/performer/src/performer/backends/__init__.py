"""Backend factory — maps AGENT_BACKEND names to adapter classes."""
from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from performer.backends.base import BackendAdapter


class UnsupportedBackendError(ValueError):
    """Raised when an unknown AGENT_BACKEND value is requested."""


#: The supported backend names, mapped to ``(module, class)``.
#:
#: Module-level on purpose: this is the single source of truth for "is this a real
#: harness name?", and callers outside the factory need to ask that question without
#: instantiating anything. Spec 161 needs it to reject a typo'd harness in a benchmark
#: search space at load time rather than mid-sweep; the coordinare config schema does not
#: constrain ``backend``, so nothing else would catch it.
#:
#: It also makes ``get_backend``'s docstring true — it referenced SUPPORTED_BACKENDS for
#: a long time while the dict was function-local and no such name existed.
SUPPORTED_BACKENDS: dict[str, tuple[str, str]] = {
    "opencode": ("performer.backends.opencode", "OpenCodeAdapter"),
    "opencode_compat": (
        "performer.backends.opencode_compat",
        "OpenCodeCompatAdapter",
    ),
    "junie": ("performer.backends.junie", "JunieBackend"),
    "claude_code": ("performer.backends.claude_code", "ClaudeCodeBackend"),
    "codex": ("performer.backends.codex", "CodexBackend"),
    "hermes": ("performer.backends.hermes", "HermesBackend"),
    "pi": ("performer.backends.pi", "PiBackend"),
    "openclaw": ("performer.backends.openclaw", "OpenClawBackend"),
    "prime_agent": ("performer.backends.prime_agent", "PrimeAgentBackend"),
    "driver": ("performer.backends.driver", "DriverBackend"),
}


def get_backend(name: str) -> "BackendAdapter":
    """Return a new BackendAdapter instance for *name*.

    Raises UnsupportedBackendError if *name* is not in SUPPORTED_BACKENDS.
    """
    # Import only the requested backend class on demand so optional
    # backend dependencies do not break unrelated configurations/tests.
    supported_backends = SUPPORTED_BACKENDS
    target = supported_backends.get(name)
    if target is None:
        supported = ", ".join(sorted(supported_backends))
        raise UnsupportedBackendError(
            f"unsupported backend {name!r}; supported: {supported}",
        )
    supported = ", ".join(sorted(supported_backends))
    module_name, class_name = target
    try:
        module = import_module(module_name)
        cls = getattr(module, class_name, None)
    except ImportError as exc:
        raise UnsupportedBackendError(
            f"backend {name!r} could not be loaded from module {module_name!r}; "
            f"supported: {supported}",
        ) from exc
    if cls is None:
        raise UnsupportedBackendError(
            f"backend {name!r} is misconfigured: class {class_name!r} "
            f"not found in module {module_name!r}; supported: {supported}",
        )
    return cls()
