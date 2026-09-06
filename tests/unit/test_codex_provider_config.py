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
            "CODEX_PROVIDER_BASE_URL": "https://litellm.example/v1",
            "CODEX_PROVIDER_NAME": "vivi",
            "CODEX_PROVIDER_WIRE_API": "responses",
        }
    )
    assert toml is not None
    assert 'model_provider = "vivi"' in toml
    assert "[model_providers.vivi]" in toml
    assert 'base_url = "https://litellm.example/v1"' in toml
    assert 'wire_api = "responses"' in toml


def test_unset_wire_api_omits_the_line() -> None:
    """The old default wrote the INVALID literal "chat"; now the line is omitted."""
    toml = _build_provider_config_toml(
        {
            "CODEX_PROVIDER_BASE_URL": "https://litellm.example/v1",
            "CODEX_PROVIDER_NAME": "vivi",
        }
    )
    assert toml is not None
    assert "wire_api" not in toml
    # the rest of the provider block is still present and valid
    assert 'model_provider = "vivi"' in toml
    assert 'base_url = "https://litellm.example/v1"' in toml


def test_never_emits_invalid_chat_literal() -> None:
    """Regression guard: the builder must never write wire_api = "chat" again."""
    toml = _build_provider_config_toml(
        {"CODEX_PROVIDER_BASE_URL": "https://litellm.example/v1"}
    )
    assert toml is not None
    assert 'wire_api = "chat"' not in toml


# --- stream idle timeout and retries (2026-09-06) ---------------------------
#
# A live architect round sat on ONE in-flight model call for over an hour while
# the gateway answered fresh probes in a second. codex has no default idle
# timeout on a custom provider, so a wedged response is waited on until the
# performer's 7200 s ceiling reaps the whole round. Two architects died that
# way in one day. The provider block now always carries an idle timeout and
# bounded retries, overridable from the endpoint env.


def _toml(env: dict[str, str] | None = None) -> str:
    base = {"CODEX_PROVIDER_BASE_URL": "https://litellm.example/v1", "CODEX_PROVIDER_NAME": "vivi"}
    base.update(env or {})
    out = _build_provider_config_toml(base)
    assert out is not None
    return out


def test_stream_idle_timeout_and_retries_default_to_bounded_values() -> None:
    toml = _toml()
    assert "stream_idle_timeout_ms = 300000" in toml
    assert "request_max_retries = 4" in toml
    assert "stream_max_retries = 5" in toml


def test_timeout_and_retries_are_written_inside_the_provider_table() -> None:
    """They must follow the [model_providers.<name>] header, or codex reads them
    as top-level keys and silently ignores them."""
    toml = _toml()
    header = toml.index("[model_providers.vivi]")
    assert toml.index("stream_idle_timeout_ms") > header
    assert toml.index("request_max_retries") > header
    assert toml.index("stream_max_retries") > header


def test_endpoint_env_overrides_the_defaults() -> None:
    toml = _toml({
        "CODEX_STREAM_IDLE_TIMEOUT_MS": "120000",
        "CODEX_REQUEST_MAX_RETRIES": "2",
        "CODEX_STREAM_MAX_RETRIES": "1",
    })
    assert "stream_idle_timeout_ms = 120000" in toml
    assert "request_max_retries = 2" in toml
    assert "stream_max_retries = 1" in toml


def test_non_integer_or_negative_overrides_are_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        _toml({"CODEX_STREAM_IDLE_TIMEOUT_MS": "five minutes"})
    with pytest.raises(ValueError):
        _toml({"CODEX_REQUEST_MAX_RETRIES": "-1"})
    with pytest.raises(ValueError):
        _toml({"CODEX_STREAM_IDLE_TIMEOUT_MS": "0"})


def test_retry_counts_accept_zero_and_the_timeout_accepts_its_floor() -> None:
    """Zero retries is a legitimate operator choice (fail on the first idle
    stream); the timeout floor is one millisecond. Both are boundaries a
    careless tightening of the minimums would break silently."""
    toml = _toml({
        "CODEX_REQUEST_MAX_RETRIES": "0",
        "CODEX_STREAM_MAX_RETRIES": "0",
        "CODEX_STREAM_IDLE_TIMEOUT_MS": "1",
    })
    assert "request_max_retries = 0" in toml
    assert "stream_max_retries = 0" in toml
    assert "stream_idle_timeout_ms = 1" in toml


def test_generated_toml_parses_with_the_keys_inside_the_provider_table() -> None:
    import tomllib

    doc = tomllib.loads(_toml({"CODEX_PROVIDER_WIRE_API": "responses"}))
    provider = doc["model_providers"]["vivi"]
    assert provider["stream_idle_timeout_ms"] == 300000
    assert provider["request_max_retries"] == 4
    assert provider["stream_max_retries"] == 5
    assert provider["wire_api"] == "responses"
    assert "stream_idle_timeout_ms" not in doc  # not a top-level key
