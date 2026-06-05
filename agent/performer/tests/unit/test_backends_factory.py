"""Spec 077 T004 — backend factory contract (contracts/backend-provider-routing.md C-1).

Asserts get_backend resolves known backends and raises a clear
UnsupportedBackendError (naming the supported set) for unknown names. The `pi`
case is xfail until T014-T015 land the adapter + registration.
"""
from __future__ import annotations

import pytest

from performer.backends import UnsupportedBackendError, get_backend


def test_get_backend_resolves_known_backend() -> None:
    """A registered backend name returns its adapter instance (lazy import)."""
    from performer.backends.codex import CodexBackend

    adapter = get_backend("codex")
    assert isinstance(adapter, CodexBackend)


def test_get_backend_unknown_raises_naming_supported_set() -> None:
    """An unknown name raises UnsupportedBackendError listing the supported
    backends — the FR-007 actionable-error contract (no silent failure)."""
    with pytest.raises(UnsupportedBackendError) as exc:
        get_backend("does-not-exist")
    msg = str(exc.value)
    assert "does-not-exist" in msg
    # Names the supported set so the operator can self-correct.
    for known in ("codex", "claude_code", "hermes", "junie", "opencode"):
        assert known in msg


def test_get_backend_resolves_pi() -> None:
    """077 T015: `pi` resolves to a PiBackend instance."""
    from performer.backends.pi import PiBackend

    adapter = get_backend("pi")
    assert isinstance(adapter, PiBackend)


def test_get_backend_resolves_openclaw() -> None:
    """077 US6: `openclaw` resolves to an OpenClawBackend instance."""
    from performer.backends.openclaw import OpenClawBackend

    adapter = get_backend("openclaw")
    assert isinstance(adapter, OpenClawBackend)
