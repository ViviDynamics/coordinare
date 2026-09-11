"""095 (T004/T005): env-signature matcher — infra/environment failure detection.

`match_env_signature` is pure: it maps a normalized failure reason to an
``EnvCause`` (pattern_id, cause, action) when it matches a built-in or
operator-supplied infra pattern, else ``None`` (fail-safe — FR-002/FR-003).

368: Model-based judgment via `match_env_signature_with_model` augments regex
matching with LLM classification when patterns don't match (fail-safe).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from coordinare.config import EnvSignaturePattern
from coordinare.services.env_signature import (
    EnvCause,
    match_env_signature,
    match_env_signature_with_model,
)


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


@pytest.mark.parametrize(
    "reason",
    [
        # the live signature: setup-ruby dies at the tool-cache mkdir
        "##[error]Error: EACCES: permission denied, mkdir '/opt/hostedtoolcache'",
        "run ruby/setup-ruby@v1 error: eacces: permission denied, mkdir '/opt/hostedtoolcache'",
        # generic tool-cache permission variants
        "permission denied writing to RUNNER_TOOL_CACHE",
        "EACCES on AGENT_TOOLSDIRECTORY",
    ],
)
def test_runner_toolcache_perm_matches(reason: str) -> None:
    # 118: a self-hosted-runner setup/tool-cache permission failure is infra, not code.
    cause = match_env_signature(reason, [])
    assert isinstance(cause, EnvCause)
    assert cause.pattern_id == "runner_toolcache_perm"
    assert "hostedtoolcache" in cause.action.lower() or "tool" in cause.action.lower()


@pytest.mark.parametrize(
    "reason",
    [
        # 118 false-positive guards (adversarial review): a BARE hostedtoolcache
        # mention with no permission/error qualifier is a SUCCESS log, not infra —
        # holding these would wrongly stall legit work.
        "##[info] found hostedtoolcache for ruby 3.2.0",
        "setup ruby: tool cache directory: /opt/hostedtoolcache",
        "python setup cached in hostedtoolcache",
        "copy /opt/hostedtoolcache/ruby into image",
        # ordinary code failures (no tool-cache token at all)
        "rubocop: 3 offenses detected",
        "undefined method `foo' for nil:NilClass",
        # a generic app-level "permission denied" with no GH tool-cache token
        "permission denied: cannot write /app/tmp/cache",
    ],
)
def test_runner_toolcache_perm_does_not_false_positive(reason: str) -> None:
    assert match_env_signature(reason, []) is None


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


# 368: Model-based judgment tests


@pytest.mark.asyncio
async def test_model_judgment_fast_path_regex_match() -> None:
    """Regex matches should return immediately without model call (fast path)."""
    backend = AsyncMock()
    reason = "artifact storage quota has been hit"
    result = await match_env_signature_with_model(reason, [], backend)
    assert result is not None
    assert result.pattern_id == "artifact_storage_quota"
    # Backend should NOT have been called
    backend.prompt.assert_not_called()


@pytest.mark.asyncio
async def test_model_judgment_no_backend() -> None:
    """When backend is None, return None (fail-safe)."""
    reason = "some novel failure we don't recognize"
    result = await match_env_signature_with_model(reason, [], backend=None)
    assert result is None


@pytest.mark.asyncio
async def test_model_judgment_backend_failure() -> None:
    """When backend raises an exception, return None (fail-safe)."""
    backend = AsyncMock()
    backend.prompt.side_effect = RuntimeError("model service unavailable")
    reason = "some novel failure we don't recognize"
    result = await match_env_signature_with_model(reason, [], backend)
    assert result is None


@pytest.mark.asyncio
async def test_model_judgment_bad_response() -> None:
    """When backend returns non-JSON or non-dict, return None (fail-safe)."""
    backend = AsyncMock()
    backend.prompt.return_value = {"text": "not json", "data": None}
    reason = "some novel failure"
    result = await match_env_signature_with_model(reason, [], backend)
    assert result is None


@pytest.mark.asyncio
async def test_model_judgment_environmental_detected() -> None:
    """Model detects environmental failure; should return EnvCause."""
    backend = AsyncMock()
    backend.prompt.return_value = {
        "text": '{"is_environmental": true, "pattern_id": "quota", "reason": "storage exhausted"}',
        "data": {"is_environmental": True, "pattern_id": "quota", "reason": "storage exhausted"},
    }
    reason = "some novel infrastructure failure"
    result = await match_env_signature_with_model(reason, [], backend)
    assert result is not None
    assert "model:" in result.pattern_id or result.pattern_id == "model:quota"
    assert "Environmental" in result.cause


@pytest.mark.asyncio
async def test_model_judgment_not_environmental() -> None:
    """Model says NOT environmental; should return None."""
    backend = AsyncMock()
    backend.prompt.return_value = {
        "text": '{"is_environmental": false, "pattern_id": "test_failure", "reason": "assertion failed"}',
        "data": {"is_environmental": False, "pattern_id": "test_failure", "reason": "assertion failed"},
    }
    reason = "expected '0px' but got '24px'"
    result = await match_env_signature_with_model(reason, [], backend)
    assert result is None


@pytest.mark.asyncio
async def test_model_judgment_mutation_must_check_is_environmental() -> None:
    """MUTATION TEST: Verify that is_environmental flag is actually checked.

    If the code ignores is_environmental and always returns a result,
    this test should FAIL.
    """
    backend = AsyncMock()
    backend.prompt.return_value = {
        "text": '{"is_environmental": false}',
        "data": {"is_environmental": False},
    }
    reason = "any test failure"
    result = await match_env_signature_with_model(reason, [], backend)
    # Must return None when is_environmental is False
    assert result is None, "is_environmental flag must be respected"


@pytest.mark.asyncio
async def test_model_judgment_mutation_must_call_backend() -> None:
    """MUTATION TEST: Verify that backend is actually called when no regex match.

    If the code skips the backend call, this test should FAIL.
    """
    backend = AsyncMock()
    backend.prompt.return_value = {
        "data": {"is_environmental": True, "pattern_id": "test"}
    }
    reason = "completely unknown failure type"
    result = await match_env_signature_with_model(reason, [], backend)
    # Backend should have been called
    backend.prompt.assert_called_once()
    assert result is not None


@pytest.mark.asyncio
async def test_model_judgment_mutation_must_check_backend_is_none() -> None:
    """MUTATION TEST: Verify backend=None is handled correctly.

    If the code ignores the None check, this test should FAIL.
    """
    reason = "any novel failure"
    result = await match_env_signature_with_model(reason, [], backend=None)
    # Must return None when backend is None
    assert result is None, "backend=None must return None without calling None"
