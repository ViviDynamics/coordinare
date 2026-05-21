"""Contract test for UpstreamHTTPError envelope (spec 067).

Covers the four obligations in
``specs/067-compatibility-first-backend/contracts/upstream_http_error.md``:
round-trip, truncation boundary, base_url credential stripping, kind
discriminator fixed.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from coordinare.upstream_errors import (
    BODY_CAP_BYTES,
    TRUNCATION_SUFFIX,
    UpstreamHTTPError,
    strip_base_url_credentials,
    truncate_body,
)

pytestmark = pytest.mark.contract


def _sample_envelope(**overrides) -> dict:
    base = {
        "kind": "upstream_http_error",
        "status": 400,
        "body": '{"error":"context length exceeded"}',
        "body_truncated": False,
        "route": "POST /v1/chat/completions",
        "base_url": "http://localhost:1234/v1",
        "upstream_request_id": None,
        "elapsed_ms": 142,
        "occurred_at": datetime(2026, 5, 20, 18, 14, 9, tzinfo=UTC),
    }
    base.update(overrides)
    return base


def test_roundtrip_every_field():
    envelope = _sample_envelope(upstream_request_id="req_abc")
    model = UpstreamHTTPError(**envelope)
    serialized = model.model_dump(mode="json")
    rebuilt = UpstreamHTTPError(**serialized)
    assert rebuilt == model


def test_truncation_at_cap_boundary():
    # Body at exactly BODY_CAP_BYTES bytes — not truncated.
    raw_at_cap = "x" * BODY_CAP_BYTES
    body, was_truncated = truncate_body(raw_at_cap)
    assert was_truncated is False
    assert body == raw_at_cap
    # One byte over — truncated with suffix; final length ≤ cap.
    raw_over = "x" * (BODY_CAP_BYTES + 1)
    body, was_truncated = truncate_body(raw_over)
    assert was_truncated is True
    assert body.endswith(TRUNCATION_SUFFIX)
    assert len(body.encode("utf-8")) <= BODY_CAP_BYTES


def test_base_url_userinfo_and_api_key_stripped():
    stripped = strip_base_url_credentials("https://user:pass@api.example.com/v1?api_key=sk-secret&model=foo")
    assert "user" not in stripped
    assert "pass" not in stripped
    assert "sk-secret" not in stripped
    assert "api_key" not in stripped
    assert "model=foo" in stripped
    assert stripped.startswith("https://api.example.com/v1")


def test_kind_discriminator_fixed():
    with pytest.raises(ValidationError):
        UpstreamHTTPError(**_sample_envelope(kind="other_envelope_type"))


def test_status_range_validated():
    with pytest.raises(ValidationError):
        UpstreamHTTPError(**_sample_envelope(status=99))
    with pytest.raises(ValidationError):
        UpstreamHTTPError(**_sample_envelope(status=600))


def test_elapsed_ms_non_negative():
    with pytest.raises(ValidationError):
        UpstreamHTTPError(**_sample_envelope(elapsed_ms=-1))
