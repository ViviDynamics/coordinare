"""Spec 234: an older performer image is refused for a consumer with frozen provenance, never run.

The job payload is ``extra="ignore"`` on the performer, so an old image would silently drop the frozen
steps and fall back to the cache's own scripts. The daemon therefore refuses before the job is posted,
using the capability the performer reports on ``GET /status``.
"""

from __future__ import annotations

from coordinare.models.performer_endpoint import PerformerStatus
from coordinare.services.env_provenance import (
    PROVENANCE_CAPABILITY_VERSION,
    image_capability_refusal,
)

OLD_IMAGE_REASON = "performer image too old for env-cache provenance (#234)"


def test_an_image_without_the_capability_is_refused_when_provenance_is_sent() -> None:
    assert image_capability_refusal(provenance_sent=True, performer_version=0) == OLD_IMAGE_REASON


def test_an_image_with_the_capability_is_accepted() -> None:
    assert PROVENANCE_CAPABILITY_VERSION == 1
    assert (
        image_capability_refusal(
            provenance_sent=True, performer_version=PROVENANCE_CAPABILITY_VERSION,
        )
        is None
    )


def test_no_provenance_sent_needs_no_capability() -> None:
    assert image_capability_refusal(provenance_sent=False, performer_version=0) is None


def test_status_from_an_old_image_reads_the_capability_as_zero() -> None:
    old_status = {
        "availability": "idle",
        "capabilities": {"backends": ["claude_code"], "tool_flags": []},
        "auth_enabled": False,
    }
    status = PerformerStatus.model_validate(old_status)
    assert status.env_provenance_version == 0


def test_status_from_a_new_image_reads_the_capability() -> None:
    new_status = {
        "availability": "idle",
        "capabilities": {"backends": ["claude_code"], "tool_flags": []},
        "auth_enabled": False,
        "env_provenance_version": 1,
    }
    status = PerformerStatus.model_validate(new_status)
    assert status.env_provenance_version == 1
