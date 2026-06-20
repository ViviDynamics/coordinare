"""095 (T004/T005): env-signature matcher — infra/environment failure detection.

`match_env_signature` is pure: it maps a normalized failure reason to an
``EnvCause`` (pattern_id, cause, action) when it matches a built-in or
operator-supplied infra pattern, else ``None`` (fail-safe — FR-002/FR-003).
"""
from __future__ import annotations

import pytest

from coordinare.config import EnvSignaturePattern
from coordinare.services.env_signature import EnvCause, match_env_signature


@pytest.mark.parametrize(
    "reason",
    [
        "artifact storage quota has been hit",
        "failed to createartifact: artifact storage quota has been hit",
        "##[error]failed to createartifact: artifact storage quota has been hit. usage is recalculated every 6-12 hours.",
    ],
)
def test_artifact_storage_quota_matches(reason: str) -> None:
    cause = match_env_signature(reason, [])
    assert isinstance(cause, EnvCause)
    assert cause.pattern_id == "artifact_storage_quota"
    assert "storage" in cause.action.lower() or "artifact" in cause.action.lower()


def test_runner_offline_matches() -> None:
    cause = match_env_signature("this check was cancelled because no runner came online", [])
    assert cause is not None and cause.pattern_id == "runner_offline"


def test_billing_limit_matches() -> None:
    cause = match_env_signature("the job was not started because spending limit reached", [])
    assert cause is not None and cause.pattern_id == "billing_limit"


def test_non_infra_reason_returns_none() -> None:
    """SC-005: a normal test failure must NOT match (no false ENV_BLOCKED)."""
    assert match_env_signature("expected '0px' but got '24px' (rspec failure)", []) is None
    assert match_env_signature("2 examples, 1 failure", []) is None


def test_operator_pattern_extends_builtins() -> None:
    custom = [
        EnvSignaturePattern(
            id="registry_outage",
            regex=r"registry.*unavailable|pull access denied",
            cause="Container registry unavailable",
            action="Check registry status / credentials",
        )
    ]
    cause = match_env_signature("docker pull failed: registry is unavailable", custom)
    assert cause is not None and cause.pattern_id == "registry_outage"
    # built-ins still apply alongside operator patterns
    assert match_env_signature("artifact storage quota has been hit", custom).pattern_id == "artifact_storage_quota"
