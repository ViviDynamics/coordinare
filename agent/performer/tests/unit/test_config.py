"""Unit tests for performer.config — constitution Principle II."""
from __future__ import annotations

import pytest

from performer.config import Settings, get_settings


class TestSettings:
    def test_default_agent_backend(self) -> None:
        s = Settings()
        assert s.AGENT_BACKEND == "opencode"

    def test_default_agent_timeout(self) -> None:
        s = Settings()
        assert s.AGENT_TIMEOUT == 1800

    def test_env_override_agent_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_BACKEND", "claude-code")
        s = Settings()
        assert s.AGENT_BACKEND == "claude-code"

    def test_env_override_agent_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AGENT_TIMEOUT", "600")
        s = Settings()
        assert s.AGENT_TIMEOUT == 600


class TestGetSettings:
    def test_returns_same_instance(self) -> None:
        get_settings.cache_clear()
        a = get_settings()
        b = get_settings()
        assert a is b

    def test_cache_clear_causes_new_instance(self) -> None:
        get_settings.cache_clear()
        a = get_settings()
        get_settings.cache_clear()
        b = get_settings()
        # Different object after clearing cache
        assert a is not b

    def test_cache_clear_picks_up_env_change(self, monkeypatch: pytest.MonkeyPatch) -> None:
        get_settings.cache_clear()
        monkeypatch.setenv("AGENT_BACKEND", "first-backend")
        first = get_settings()
        assert first.AGENT_BACKEND == "first-backend"

        get_settings.cache_clear()
        monkeypatch.setenv("AGENT_BACKEND", "second-backend")
        second = get_settings()
        assert second.AGENT_BACKEND == "second-backend"


# ---------------------------------------------------------------------------
# 036 — GitHub Enterprise Support: GITHUB_API_URL setting
# ---------------------------------------------------------------------------


class TestGitHubAPIURLSetting:
    """Tests for the GITHUB_API_URL performer setting (036)."""

    def test_default_github_api_url(self) -> None:
        s = Settings()
        assert s.GITHUB_API_URL == "https://api.github.com"

    def test_env_override_github_api_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITHUB_API_URL", "https://github.acme.corp/api/v3")
        s = Settings()
        assert s.GITHUB_API_URL == "https://github.acme.corp/api/v3"

    def test_get_settings_picks_up_github_api_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        get_settings.cache_clear()
        monkeypatch.setenv("GITHUB_API_URL", "https://ghes.internal/api/v3")
        s = get_settings()
        assert s.GITHUB_API_URL == "https://ghes.internal/api/v3"
        get_settings.cache_clear()
