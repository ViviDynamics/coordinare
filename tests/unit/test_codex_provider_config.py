"""082 US9 / FR-011 — codex config.toml provider-block builder.

codex 0.137.0 rejects every chat-flavoured ``wire_api`` string ("chat",
"chat_completions", "completions", …) as an invalid enum variant: it swallows the
config error, falls back to a default config lacking the custom provider, and dies
with a misleading "Model provider not found". The only accepted explicit value is
``responses``. When ``CODEX_PROVIDER_WIRE_API`` is unset the builder must OMIT the
``wire_api`` line entirely (so the app-server can START — it rejects the literal
``"chat"`` the old default produced). NOTE: omitting ``wire_api`` does NOT make codex
default to chat-completions — codex 0.137.0 is hard-locked to the Responses API at
request time; this builder only governs whether the app-server boots, not which wire
codex calls. Serving ``/responses`` in the proxy is the separate, still-open fix.
"""
from __future__ import annotations

from performer.backends.codex import _build_provider_config_toml


def test_no_base_url_returns_none() -> None:
    """No CODEX_PROVIDER_BASE_URL → no override active → no config.toml."""
    assert _build_provider_config_toml({}) is None


def test_explicit_responses_wire_api_is_written() -> None:
    toml = _build_provider_config_toml(
        {
            "CODEX_PROVIDER_BASE_URL": "https://litellm.vividynamics.com/v1",
            "CODEX_PROVIDER_NAME": "vivi",
            "CODEX_PROVIDER_WIRE_API": "responses",
        }
    )
    assert toml is not None
    assert 'model_provider = "vivi"' in toml
    assert "[model_providers.vivi]" in toml
    assert 'base_url = "https://litellm.vividynamics.com/v1"' in toml
    assert 'wire_api = "responses"' in toml


def test_unset_wire_api_omits_the_line() -> None:
    """The old default wrote the INVALID literal "chat"; now the line is omitted."""
    toml = _build_provider_config_toml(
        {
            "CODEX_PROVIDER_BASE_URL": "https://litellm.vividynamics.com/v1",
            "CODEX_PROVIDER_NAME": "vivi",
        }
    )
    assert toml is not None
    assert "wire_api" not in toml
    # the rest of the provider block is still present and valid
    assert 'model_provider = "vivi"' in toml
    assert 'base_url = "https://litellm.vividynamics.com/v1"' in toml


def test_never_emits_invalid_chat_literal() -> None:
    """Regression guard: the builder must never write wire_api = "chat" again."""
    toml = _build_provider_config_toml(
        {"CODEX_PROVIDER_BASE_URL": "https://litellm.vividynamics.com/v1"}
    )
    assert toml is not None
    assert 'wire_api = "chat"' not in toml
