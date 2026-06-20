"""Regression: _probe_url is /v1-aware for OpenAI-wire targets.

A normalize target fronting a verbatim-POST client (junie) declares a BARE-ORIGIN
base_url (the shim forwards base + the client's full /v1 path). The startup health
probe must therefore hit ``base/v1/chat/completions`` — not ``base/chat/completions``,
which 404s an Ollama-style upstream that is actually up (live block, 2026-06-20).
A base that already ends in /v1 (openclaw/claude) keeps the single suffix.
"""
from __future__ import annotations

from performer.proxy.health import _probe_url
from performer.proxy.routing import TargetDescriptor


def _t(base_url: str, **over) -> TargetDescriptor:
    kw = dict(
        base_url=base_url,
        wire_format="openai",
        strategy="normalize",
        normalizers=["strip_control_chars", "strip_reasoning"],
    )
    kw.update(over)
    return TargetDescriptor(**kw)


def test_bare_origin_openai_probe_gets_v1() -> None:
    assert _probe_url(_t("http://192.168.3.30:11434")) == "http://192.168.3.30:11434/v1/chat/completions"


def test_v1_terminated_openai_probe_keeps_single_v1() -> None:
    # openclaw-style reroute base already ends in /v1 — must not double it.
    t = _t("http://192.168.3.30:11434/v1", strategy="reroute", normalizers=[])
    assert _probe_url(t) == "http://192.168.3.30:11434/v1/chat/completions"


def test_trailing_slash_normalized() -> None:
    assert _probe_url(_t("http://192.168.3.30:11434/")) == "http://192.168.3.30:11434/v1/chat/completions"
