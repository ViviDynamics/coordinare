"""Unit tests for HTTPPerformerService._log_upstream_http_error (spec 067 FR-004)."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services.http_performer_service import HTTPPerformerService


def _cfg() -> PerformerEndpointConfig:
    return PerformerEndpointConfig.model_validate(
        {
            "id": "perf-p1",
            "mode": "persistent",
            "roles": ["implementing"],
            "image": "performer:base",
            "endpoint": "http://127.0.0.1:8080",
        },
    )


def _envelope(status: int = 502) -> dict[str, object]:
    return {
        "kind": "upstream_http_error",
        "status": status,
        "body": "upstream is broken",
        "body_truncated": False,
        "route": "/v1/messages",
        "base_url": "https://user:pw@upstream.example/v1?api_key=secret",
        "upstream_request_id": "req-abc",
        "elapsed_ms": 42,
        "occurred_at": datetime.now(UTC).isoformat(),
    }


def test_log_upstream_no_metrics_is_noop(caplog: pytest.LogCaptureFixture) -> None:
    svc = HTTPPerformerService(_cfg())
    svc._log_upstream_http_error({})  # no "metrics" key
    svc._log_upstream_http_error({"metrics": "not-a-dict"})
    svc._log_upstream_http_error({"metrics": {}})
    svc._log_upstream_http_error({"metrics": {"upstream_http_error": "not-a-dict"}})


def test_log_upstream_transient_emits_warning(caplog: pytest.LogCaptureFixture) -> None:
    svc = HTTPPerformerService(_cfg())
    response = {"metrics": {"upstream_http_error": _envelope(status=503)}}
    with caplog.at_level("WARNING"):
        svc._log_upstream_http_error(response)
    # Should not raise; we just exercise the code path.


def test_log_upstream_permanent_emits_error(caplog: pytest.LogCaptureFixture) -> None:
    svc = HTTPPerformerService(_cfg())
    response = {"metrics": {"upstream_http_error": _envelope(status=400)}}
    with caplog.at_level("WARNING"):
        svc._log_upstream_http_error(response)


def test_log_upstream_malformed_envelope_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    svc = HTTPPerformerService(_cfg())
    bad = {"kind": "upstream_http_error", "status": "not-an-int"}
    with caplog.at_level("WARNING"):
        svc._log_upstream_http_error({"metrics": {"upstream_http_error": bad}})
