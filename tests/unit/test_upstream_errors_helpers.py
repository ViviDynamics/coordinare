"""Unit tests for upstream_errors helper functions."""
from __future__ import annotations

from coordinare.upstream_errors import (
    BODY_CAP_BYTES,
    TRUNCATION_SUFFIX,
    strip_base_url_credentials,
    truncate_body,
)


def test_strip_base_url_credentials_removes_userinfo() -> None:
    assert (
        strip_base_url_credentials("https://user:pw@host.example/v1/messages")
        == "https://host.example/v1/messages"
    )


def test_strip_base_url_credentials_preserves_port_and_path() -> None:
    assert (
        strip_base_url_credentials("https://user:pw@host.example:8443/v1?foo=bar")
        == "https://host.example:8443/v1?foo=bar"
    )


def test_strip_base_url_credentials_drops_api_key_query() -> None:
    out = strip_base_url_credentials(
        "https://h.example/v1?api_key=secret&foo=bar&API_KEY=other",
    )
    assert "secret" not in out
    assert "other" not in out
    assert "foo=bar" in out


def test_strip_base_url_credentials_handles_empty_host() -> None:
    # No host (relative-ish input) — should not crash.
    assert strip_base_url_credentials("/v1/messages") == "/v1/messages"


def test_truncate_body_short_passthrough() -> None:
    body, was = truncate_body("hello")
    assert body == "hello"
    assert was is False


def test_truncate_body_long_truncates_with_suffix() -> None:
    raw = "x" * (BODY_CAP_BYTES + 500)
    body, was = truncate_body(raw)
    assert was is True
    assert body.endswith(TRUNCATION_SUFFIX)
    assert len(body.encode("utf-8")) <= BODY_CAP_BYTES


def test_truncate_body_exactly_at_cap_not_truncated() -> None:
    raw = "y" * BODY_CAP_BYTES
    body, was = truncate_body(raw)
    assert was is False
    assert body == raw
