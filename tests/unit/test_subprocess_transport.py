"""Unit tests for SubprocessTransport._build_subprocess_env (051)."""
from __future__ import annotations

from typing import ClassVar

import pytest

from coordinare.transport.subprocess_transport import SubprocessTransport


class _Identity:
    def __init__(self, name: str, email: str) -> None:
        self.name = name
        self.email = email


class _BotConfig:
    bot_identity = _Identity("coordinare-bot", "bot@example.com")
    env_passthrough: ClassVar[list[str]] = []


class _PassthroughConfig:
    bot_identity = _Identity("Coordinare Bot", "coordinare@localhost")
    env_passthrough: ClassVar[list[str]] = ["ANTHROPIC_API_KEY"]


def _make_transport(config=None, github_token=None) -> SubprocessTransport:
    return SubprocessTransport("echo", 30, config=config, github_token=github_token)


def test_subprocess_env_excludes_arbitrary_host_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Arbitrary host vars are NOT present in the subprocess env."""
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "supersecret")
    env = _make_transport(config=_BotConfig())._build_subprocess_env()
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_subprocess_env_injects_provided_github_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """GITHUB_TOKEN in subprocess env comes from the provided token, not host."""
    monkeypatch.setenv("GITHUB_TOKEN", "host-personal-token")
    env = _make_transport(config=_BotConfig(), github_token="ghs_app_token")._build_subprocess_env()
    assert env["GITHUB_TOKEN"] == "ghs_app_token"


def test_subprocess_env_uses_bot_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """GIT_AUTHOR_NAME/EMAIL come from bot_identity config, not host git config."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Human Dev")
    env = _make_transport(config=_BotConfig())._build_subprocess_env()
    assert env["GIT_AUTHOR_NAME"] == "coordinare-bot"
    assert env["GIT_AUTHOR_EMAIL"] == "bot@example.com"
    assert env["GIT_COMMITTER_NAME"] == "coordinare-bot"
    assert env["GIT_COMMITTER_EMAIL"] == "bot@example.com"


def test_subprocess_env_passthrough_copies_named_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vars listed in env_passthrough are copied when present on host."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    env = _make_transport(config=_PassthroughConfig())._build_subprocess_env()
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-test"


def test_subprocess_env_defaults_without_config() -> None:
    """No config → defaults used; no crash."""
    env = _make_transport(config=None)._build_subprocess_env()
    assert env["GIT_AUTHOR_NAME"] == "Coordinare Bot"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
