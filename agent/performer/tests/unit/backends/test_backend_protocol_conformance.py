"""Statically verify that every BackendAdapter implementation accepts the same
keyword arguments as the base protocol's start() signature.

This catches the class of bug where a new parameter is added to BackendAdapter.start()
but a concrete backend is not updated, causing a runtime TypeError on dispatch.
"""
from __future__ import annotations

import inspect

import pytest
from performer.backends.base import BackendAdapter
from performer.backends.claude_code import ClaudeCodeBackend
from performer.backends.codex import CodexBackend
from performer.backends.junie import JunieBackend
from performer.backends.opencode import OpenCodeAdapter


def _kwonly_params(method) -> dict[str, inspect.Parameter]:
    """Return the keyword-only parameters of a method, excluding 'self'."""
    sig = inspect.signature(method)
    return {
        name: param
        for name, param in sig.parameters.items()
        if param.kind == inspect.Parameter.KEYWORD_ONLY
    }


# The canonical set of keyword-only params defined on the protocol.
_PROTOCOL_START_KWARGS = set(_kwonly_params(BackendAdapter.start))

_CONCRETE_BACKENDS = [
    ClaudeCodeBackend,
    CodexBackend,
    JunieBackend,
    OpenCodeAdapter,
]


@pytest.mark.parametrize("backend_cls", _CONCRETE_BACKENDS, ids=lambda c: c.__name__)
def test_start_accepts_all_protocol_kwargs(backend_cls):
    """backend.start() must accept every keyword arg defined in BackendAdapter.start()."""
    impl_params = _kwonly_params(backend_cls.start)
    missing = _PROTOCOL_START_KWARGS - set(impl_params)
    assert not missing, (
        f"{backend_cls.__name__}.start() is missing protocol kwargs: {sorted(missing)}"
    )


@pytest.mark.parametrize("backend_cls", _CONCRETE_BACKENDS, ids=lambda c: c.__name__)
def test_start_kwargs_have_none_defaults(backend_cls):
    """All keyword-only params on start() must default to None (they're optional hints)."""
    impl_params = _kwonly_params(backend_cls.start)
    bad = [
        name
        for name, param in impl_params.items()
        if name in _PROTOCOL_START_KWARGS and param.default is not None
    ]
    assert not bad, (
        f"{backend_cls.__name__}.start() has non-None defaults for: {bad}"
    )
