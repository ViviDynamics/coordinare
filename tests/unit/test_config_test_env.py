"""Unit tests for spec-092 ``TestEnvConfig`` and ``SymphonyConfig.test_env``.

Covers the load-time validation contract (contracts/test_env_config.md): exactly-one
source, repo_path containment, ``extra="forbid"``, and SymphonyConfig round-trips with
and without the block.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coordinare.config import SymphonyConfig, TestEnvConfig


class TestTestEnvConfigSource:
    """Exactly-one-source rule (repo_path XOR host_path)."""

    def test_repo_path_alone_accepted(self) -> None:
        cfg = TestEnvConfig(repo_path=".env.test")
        assert cfg.repo_path == ".env.test"
        assert cfg.host_path is None

    def test_host_path_alone_accepted(self) -> None:
        cfg = TestEnvConfig(host_path="/run/secrets/website-test.env")
        assert cfg.host_path == "/run/secrets/website-test.env"
        assert cfg.repo_path is None

    def test_both_set_rejected(self) -> None:
        with pytest.raises(ValidationError, match="exactly one"):
            TestEnvConfig(repo_path=".env.test", host_path="/run/secrets/x.env")

    def test_neither_set_rejected(self) -> None:
        with pytest.raises(ValidationError, match="empty test_env"):
            TestEnvConfig()


class TestTestEnvConfigContainment:
    """repo_path must stay inside the clone (no ``..``, not absolute)."""

    def test_repo_path_with_dotdot_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"escapes|\.\."):
            TestEnvConfig(repo_path="../secrets/.env.test")

    def test_repo_path_with_nested_dotdot_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"escapes|\.\."):
            TestEnvConfig(repo_path="config/../../secrets/.env.test")

    def test_absolute_repo_path_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"relative|absolute"):
            TestEnvConfig(repo_path="/etc/secrets/.env.test")


class TestTestEnvConfigExtraForbid:
    """``extra="forbid"`` rejects typos in field names."""

    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TestEnvConfig.model_validate({"repo_path": ".env.test", "rep_path": ".typo"})


class TestSymphonyConfigTestEnv:
    """SymphonyConfig round-trips with and without the block."""

    def test_symphony_without_test_env(self) -> None:
        sym = SymphonyConfig(name="website", github_project_number=51)
        assert sym.test_env is None

    def test_symphony_with_repo_path_test_env(self) -> None:
        sym = SymphonyConfig(
            name="website",
            github_project_number=51,
            test_env={"repo_path": ".env.test"},
        )
        assert sym.test_env is not None
        assert sym.test_env.repo_path == ".env.test"

    def test_symphony_with_host_path_test_env(self) -> None:
        sym = SymphonyConfig(
            name="website",
            github_project_number=51,
            test_env={"host_path": "/run/secrets/website-test.env"},
        )
        assert sym.test_env is not None
        assert sym.test_env.host_path == "/run/secrets/website-test.env"

    def test_symphony_round_trip_preserves_test_env(self) -> None:
        sym = SymphonyConfig(
            name="website",
            github_project_number=51,
            test_env={"repo_path": ".env.test"},
        )
        restored = SymphonyConfig.model_validate(sym.model_dump())
        assert restored.test_env is not None
        assert restored.test_env.repo_path == ".env.test"

    def test_symphony_with_invalid_test_env_block_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SymphonyConfig(
                name="website",
                github_project_number=51,
                test_env={"repo_path": ".env.test", "host_path": "/x.env"},
            )
