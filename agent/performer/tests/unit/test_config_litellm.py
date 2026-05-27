"""073: Settings fields for the LiteLLM proxy routing of claude_code."""
from __future__ import annotations

import pytest

from performer.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_defaults_are_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LITELLM_PROXY_BASE_URL", raising=False)
    monkeypatch.delenv("LITELLM_PROXY_AUTH_TOKEN", raising=False)
    s = Settings()
    assert s.LITELLM_PROXY_BASE_URL == ""
    assert s.LITELLM_PROXY_AUTH_TOKEN == ""


def test_reads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LITELLM_PROXY_BASE_URL", "https://proxy.example.com")
    monkeypatch.setenv("LITELLM_PROXY_AUTH_TOKEN", "sk-litellm-secret")
    s = Settings()
    assert s.LITELLM_PROXY_BASE_URL == "https://proxy.example.com"
    assert s.LITELLM_PROXY_AUTH_TOKEN == "sk-litellm-secret"


def test_empty_env_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """env_ignore_empty=True — empty string in env should yield the default."""
    monkeypatch.setenv("LITELLM_PROXY_BASE_URL", "")
    s = Settings()
    assert s.LITELLM_PROXY_BASE_URL == ""
