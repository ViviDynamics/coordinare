"""Unit tests for classify_upstream (spec 067 T024, FR-005).

Table-driven coverage:
  - Every status in TRANSIENT_STATUSES → "transient"
  - Representative permanent statuses (400, 401, 403, 404) → "permanent"
  - 5xx-with-marker → "transient" (body-marker fallback, scoped to 5xx)
  - 4xx-with-marker → "permanent" (marker fallback does NOT engage on 4xx)
  - Non-marker body with permanent status → "permanent"
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coordinare.graph.nodes.handle_system_error import (
    TRANSIENT_BODY_MARKERS,
    TRANSIENT_STATUSES,
    classify_upstream,
)
from coordinare.upstream_errors import UpstreamHTTPError


def _err(status: int, body: str = "boom") -> UpstreamHTTPError:
    return UpstreamHTTPError(
        status=status,
        body=body,
        body_truncated=False,
        route="/v1/chat/completions",
        base_url="https://api.example.com/v1",
        elapsed_ms=10,
        occurred_at=datetime.now(UTC),
    )


@pytest.mark.parametrize("status", sorted(TRANSIENT_STATUSES))
def test_transient_statuses_classified_transient(status: int):
    assert classify_upstream(_err(status)) == "transient"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_statuses_classified_permanent(status: int):
    assert classify_upstream(_err(status, body="model not found")) == "permanent"


@pytest.mark.parametrize("marker", TRANSIENT_BODY_MARKERS)
def test_marker_in_5xx_body_promotes_to_transient(marker: str):
    # 501 is 5xx but not in TRANSIENT_STATUSES, so the marker fallback is
    # the deciding path here.
    assert classify_upstream(_err(501, body=f"upstream {marker} please retry")) == "transient"


@pytest.mark.parametrize("marker", TRANSIENT_BODY_MARKERS)
def test_marker_in_4xx_body_stays_permanent(marker: str):
    # A 4xx that merely mentions a transient marker (e.g. a policy
    # explanation) must not be auto-retried — the fallback is 5xx-scoped.
    assert classify_upstream(_err(400, body=f"policy describes {marker} behaviour")) == "permanent"


def test_marker_match_is_case_insensitive():
    # 501 (5xx, not in TRANSIENT_STATUSES) forces the marker path.
    assert classify_upstream(_err(501, body="RATE LIMIT EXCEEDED")) == "transient"


def test_permanent_body_without_marker_stays_permanent():
    assert classify_upstream(_err(400, body="invalid request: missing field")) == "permanent"
