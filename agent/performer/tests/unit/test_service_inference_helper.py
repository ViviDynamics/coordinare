"""Unit tests for performer.main._run_service_inference (spec 063 T026c)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from performer.main import (
    DEFAULT_INFERENCE_MAX_TOKENS,
    DEFAULT_INFERENCE_MAX_TOOL_CALLS,
    _run_service_inference,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_skips_when_env_cache_path_is_empty(tmp_path: Path) -> None:
    out = await _run_service_inference(tmp_path, "")
    assert out == {"inference_skipped_reason": "no_env_cache_path"}


@pytest.mark.asyncio
async def test_skips_when_no_api_key_and_no_manual_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No .coordinare/score.json file → manual override doesn't apply.
    # No API key → LLM path skipped with no_api_key reason.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    out = await _run_service_inference(project_dir, str(cache_dir))
    # Either the coordinare package isn't importable in this environment, or
    # the LLM path is skipped for no_api_key. Both are valid "skipped" outcomes
    # that downstream code surfaces verbatim.
    assert out.get("inference_skipped_reason") in {
        "no_api_key",
        "coordinare_not_available",
    }


def _patch_llm_path(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]
) -> None:
    """Stub the LLM-path collaborators so we can assert on constructor kwargs.

    Bypasses real network/SDK use by:
    - making manual_override always say "not applied"
    - capturing ClaudeServiceLLMClient.from_api_key kwargs
    - short-circuiting infer_services to return a stub result
    """
    from coordinare_service_inference import claude_llm_client, manual_override

    class _StubClient:
        pass

    def _fake_from_api_key(**kwargs: Any) -> _StubClient:
        captured.update(kwargs)
        return _StubClient()

    class _StubOverride:
        applied = False
        manifest = None

    def _fake_apply_manual_override(*_args: Any, **_kwargs: Any) -> _StubOverride:
        return _StubOverride()

    class _StubManifest:
        services: tuple = ()
        test_env_source = None

    class _StubResult:
        attempts = 1
        succeeded = True
        manifest = _StubManifest()

    async def _fake_infer_services(**kwargs: Any) -> _StubResult:
        captured.setdefault("_infer_kwargs", {}).update(kwargs)
        return _StubResult()

    import coordinare_service_inference as svc_inf

    monkeypatch.setattr(claude_llm_client.ClaudeServiceLLMClient, "from_api_key",
                        classmethod(lambda cls, **kw: _fake_from_api_key(**kw)))
    monkeypatch.setattr(manual_override, "apply_manual_override", _fake_apply_manual_override)
    monkeypatch.setattr(svc_inf, "infer_services", _fake_infer_services)


@pytest.mark.asyncio
async def test_max_tokens_override_via_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("COORDINARE_INFERENCE_MAX_TOKENS", "32000")
    captured: dict[str, Any] = {}
    _patch_llm_path(monkeypatch, captured)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    await _run_service_inference(project_dir, str(cache_dir))

    assert captured.get("max_tokens") == 32000


@pytest.mark.asyncio
async def test_max_tokens_default_when_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("COORDINARE_INFERENCE_MAX_TOKENS", raising=False)
    captured: dict[str, Any] = {}
    _patch_llm_path(monkeypatch, captured)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    await _run_service_inference(project_dir, str(cache_dir))

    assert captured.get("max_tokens") == DEFAULT_INFERENCE_MAX_TOKENS


@pytest.mark.asyncio
async def test_max_tool_calls_override_via_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("COORDINARE_INFERENCE_MAX_TOOL_CALLS", "120")
    captured: dict[str, Any] = {}
    _patch_llm_path(monkeypatch, captured)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    await _run_service_inference(project_dir, str(cache_dir))

    assert captured["_infer_kwargs"]["max_tool_calls"] == 120


@pytest.mark.asyncio
async def test_max_tool_calls_default_when_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("COORDINARE_INFERENCE_MAX_TOOL_CALLS", raising=False)
    captured: dict[str, Any] = {}
    _patch_llm_path(monkeypatch, captured)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    await _run_service_inference(project_dir, str(cache_dir))

    assert (
        captured["_infer_kwargs"]["max_tool_calls"]
        == DEFAULT_INFERENCE_MAX_TOOL_CALLS
    )


@pytest.mark.asyncio
async def test_max_tool_calls_falls_back_when_env_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("COORDINARE_INFERENCE_MAX_TOOL_CALLS", "not-a-number")
    captured: dict[str, Any] = {}
    _patch_llm_path(monkeypatch, captured)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    await _run_service_inference(project_dir, str(cache_dir))

    assert (
        captured["_infer_kwargs"]["max_tool_calls"]
        == DEFAULT_INFERENCE_MAX_TOOL_CALLS
    )


def _patch_openai_compat_path(
    monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]
) -> None:
    """Stub the openai_compat strategy + manual_override + infer_services."""
    from coordinare_service_inference import manual_override, openai_compat_llm_client

    class _StubClient:
        pass

    def _fake_from_config(**kwargs: Any) -> _StubClient:
        captured.update(kwargs)
        return _StubClient()

    class _StubOverride:
        applied = False
        manifest = None

    def _fake_apply_manual_override(*_args: Any, **_kwargs: Any) -> _StubOverride:
        return _StubOverride()

    class _StubManifest:
        services: tuple = ()
        test_env_source = None

    class _StubResult:
        attempts = 1
        succeeded = True
        manifest = _StubManifest()

    async def _fake_infer_services(**kwargs: Any) -> _StubResult:
        captured.setdefault("_infer_kwargs", {}).update(kwargs)
        return _StubResult()

    import coordinare_service_inference as svc_inf

    monkeypatch.setattr(
        openai_compat_llm_client.OpenAICompatServiceLLMClient,
        "from_config",
        classmethod(lambda cls, **kw: _fake_from_config(**kw)),
    )
    monkeypatch.setattr(manual_override, "apply_manual_override", _fake_apply_manual_override)
    monkeypatch.setattr(svc_inf, "infer_services", _fake_infer_services)


@pytest.mark.asyncio
async def test_provider_anthropic_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("COORDINARE_INFERENCE_PROVIDER", raising=False)
    captured: dict[str, Any] = {}
    _patch_llm_path(monkeypatch, captured)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    await _run_service_inference(project_dir, str(cache_dir))

    # Anthropic from_api_key was called (max_tokens captured).
    assert "max_tokens" in captured


@pytest.mark.asyncio
async def test_provider_openai_compat_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COORDINARE_INFERENCE_PROVIDER", "openai_compat")
    monkeypatch.setenv("COORDINARE_INFERENCE_BASE_URL", "http://litellm.local:4000/v1")
    monkeypatch.setenv("COORDINARE_INFERENCE_API_KEY", "local-key")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    captured: dict[str, Any] = {}
    _patch_openai_compat_path(monkeypatch, captured)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    await _run_service_inference(project_dir, str(cache_dir))

    assert captured.get("base_url") == "http://litellm.local:4000/v1"
    assert captured.get("api_key") == "local-key"


@pytest.mark.asyncio
async def test_provider_openai_compat_missing_base_url_fails_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COORDINARE_INFERENCE_PROVIDER", "openai_compat")
    monkeypatch.delenv("COORDINARE_INFERENCE_BASE_URL", raising=False)
    # Even with ANTHROPIC_API_KEY set, must NOT silently fall back.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    captured: dict[str, Any] = {}
    _patch_openai_compat_path(monkeypatch, captured)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    out = await _run_service_inference(project_dir, str(cache_dir))

    assert out.get("inference_skipped_reason") == "openai_compat_missing_base_url"
    # Confirm we never built a client.
    assert captured == {}


@pytest.mark.asyncio
async def test_max_tokens_falls_back_when_env_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("COORDINARE_INFERENCE_MAX_TOKENS", "not-a-number")
    captured: dict[str, Any] = {}
    _patch_llm_path(monkeypatch, captured)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    await _run_service_inference(project_dir, str(cache_dir))

    assert captured.get("max_tokens") == DEFAULT_INFERENCE_MAX_TOKENS


@pytest.mark.asyncio
async def test_env_literal_placeholder_treated_as_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the coordinare's os.path.expandvars leaves an unset ${VAR} as the
    literal string, it can still leak through docker -e. The performer must
    treat literal ${...} values as unset so fallback-on-empty defaults kick in
    — otherwise COORDINARE_INFERENCE_AGENT_VERSION=${...} trips the agent_version
    regex guard and crashes env_bootstrap."""
    from performer.main import DEFAULT_INFERENCE_AGENT_VERSION

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv(
        "COORDINARE_INFERENCE_AGENT_VERSION",
        "${COORDINARE_INFERENCE_AGENT_VERSION}",
    )
    monkeypatch.setenv(
        "COORDINARE_INFERENCE_MAX_TOKENS", "${COORDINARE_INFERENCE_MAX_TOKENS}"
    )
    monkeypatch.setenv(
        "COORDINARE_INFERENCE_MAX_TOOL_CALLS",
        "${COORDINARE_INFERENCE_MAX_TOOL_CALLS}",
    )
    captured: dict[str, Any] = {}
    _patch_llm_path(monkeypatch, captured)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    out = await _run_service_inference(project_dir, str(cache_dir))

    assert out.get("inference_agent_version") == DEFAULT_INFERENCE_AGENT_VERSION
    assert captured.get("max_tokens") == DEFAULT_INFERENCE_MAX_TOKENS
    infer_kwargs = captured.get("_infer_kwargs", {})
    assert infer_kwargs.get("agent_version") == DEFAULT_INFERENCE_AGENT_VERSION
    assert infer_kwargs.get("max_tool_calls") == DEFAULT_INFERENCE_MAX_TOOL_CALLS


# ---------------------------------------------------------------------------
# 077 T025 [US4] — service-inference timeout stays NON-FATAL (backend-agnostic)
# ---------------------------------------------------------------------------
# The env_bootstrap stage runs `_run_service_inference` regardless of which
# backend executes the role (opencode for 077 US4, claude_code previously).
# The probe timing out MUST NOT fail the bootstrap — it returns a
# skipped-reason result so the env cache is still marked ready (076 T175,
# carried into main). This guards that contract for the opencode bootstrap.


@pytest.mark.asyncio
async def test_inference_timeout_is_non_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    captured: dict[str, Any] = {}
    _patch_llm_path(monkeypatch, captured)

    # Override infer_services to time out — the failsafe must catch it.
    import coordinare_service_inference as svc_inf

    async def _timing_out_infer_services(**_kwargs: Any) -> Any:
        raise TimeoutError("service inference exceeded step timeout")

    monkeypatch.setattr(svc_inf, "infer_services", _timing_out_infer_services)

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # Must NOT raise — the bootstrap continues with inference skipped.
    out = await _run_service_inference(project_dir, str(cache_dir))

    assert out.get("inference_succeeded") is not True
    reason = out.get("inference_skipped_reason") or ""
    assert "TimeoutError" in reason
