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
