"""Unit tests for spec-092 ``load_test_env`` (contracts/load_test_env.md).

Covers dotenv parse edge cases, host vs. repo source resolution, read-time containment
re-check, missing-file error attribution, no-shell-execution, and the SC-007 perf budget.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from coordinare.config import TestEnvConfig
from coordinare.services.test_env_loader import (
    TestEnvFileError,
    load_test_env,
)


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


class TestDotenvParsing:
    def test_basic_key_value(self, tmp_path: Path) -> None:
        _write(tmp_path / ".env.test", "POSTGRESQL_PASSWORD=secret-pw\n")
        out = load_test_env(TestEnvConfig(repo_path=".env.test"), repo_root=tmp_path)
        assert out == {"POSTGRESQL_PASSWORD": "secret-pw"}

    def test_double_quotes_stripped_one_layer(self, tmp_path: Path) -> None:
        _write(tmp_path / ".env.test", 'KEY="value"\n')
        out = load_test_env(TestEnvConfig(repo_path=".env.test"), repo_root=tmp_path)
        assert out == {"KEY": "value"}

    def test_single_quotes_stripped_one_layer(self, tmp_path: Path) -> None:
        _write(tmp_path / ".env.test", "KEY='value'\n")
        out = load_test_env(TestEnvConfig(repo_path=".env.test"), repo_root=tmp_path)
        assert out == {"KEY": "value"}

    def test_only_one_quote_layer_stripped(self, tmp_path: Path) -> None:
        _write(tmp_path / ".env.test", "KEY=\"'value'\"\n")
        out = load_test_env(TestEnvConfig(repo_path=".env.test"), repo_root=tmp_path)
        assert out == {"KEY": "'value'"}

    def test_leading_export_ignored(self, tmp_path: Path) -> None:
        _write(tmp_path / ".env.test", "export DATABASE_URL=postgres://x\n")
        out = load_test_env(TestEnvConfig(repo_path=".env.test"), repo_root=tmp_path)
        assert out == {"DATABASE_URL": "postgres://x"}

    def test_comments_and_blank_lines_skipped(self, tmp_path: Path) -> None:
        _write(
            tmp_path / ".env.test",
            "# a comment\n\n   # indented comment\nKEY=value\n\n",
        )
        out = load_test_env(TestEnvConfig(repo_path=".env.test"), repo_root=tmp_path)
        assert out == {"KEY": "value"}

    def test_equals_inside_value_preserved(self, tmp_path: Path) -> None:
        _write(tmp_path / ".env.test", "DATABASE_URL=postgres://u:p=q@h/db\n")
        out = load_test_env(TestEnvConfig(repo_path=".env.test"), repo_root=tmp_path)
        assert out == {"DATABASE_URL": "postgres://u:p=q@h/db"}

    def test_crlf_line_endings_tolerated(self, tmp_path: Path) -> None:
        _write(tmp_path / ".env.test", "KEY=value\r\nKEY2=value2\r\n")
        out = load_test_env(TestEnvConfig(repo_path=".env.test"), repo_root=tmp_path)
        assert out == {"KEY": "value", "KEY2": "value2"}

    def test_empty_file_returns_empty_dict(self, tmp_path: Path) -> None:
        _write(tmp_path / ".env.test", "")
        out = load_test_env(TestEnvConfig(repo_path=".env.test"), repo_root=tmp_path)
        assert out == {}

    def test_no_shell_execution_value_returned_literally(self, tmp_path: Path) -> None:
        _write(
            tmp_path / ".env.test",
            "DANGER=$(rm -rf /)\nBACKTICK=`whoami`\nINTERP=${OTHER}\n",
        )
        out = load_test_env(TestEnvConfig(repo_path=".env.test"), repo_root=tmp_path)
        assert out == {
            "DANGER": "$(rm -rf /)",
            "BACKTICK": "`whoami`",
            "INTERP": "${OTHER}",
        }


class TestSourceResolution:
    def test_host_path_read_directly(self, tmp_path: Path) -> None:
        host_file = tmp_path / "host-secrets.env"
        _write(host_file, "HOST_KEY=hv\n")
        # repo_root is unrelated for host_path resolution.
        out = load_test_env(
            TestEnvConfig(host_path=str(host_file)),
            repo_root=tmp_path / "some_repo",
        )
        assert out == {"HOST_KEY": "hv"}

    def test_repo_path_resolved_under_repo_root(self, tmp_path: Path) -> None:
        sub = tmp_path / "config"
        sub.mkdir()
        _write(sub / ".env.test", "REPO_KEY=rv\n")
        out = load_test_env(
            TestEnvConfig(repo_path="config/.env.test"), repo_root=tmp_path
        )
        assert out == {"REPO_KEY": "rv"}


class TestContainmentReassertion:
    """Defense-in-depth: re-reject escapes at read time even though config-load checked."""

    def test_repo_path_escaping_root_rejected(self, tmp_path: Path) -> None:
        # Build a TestEnvConfig that bypasses config-load validation to prove the
        # loader independently re-asserts containment.
        cfg = TestEnvConfig.model_construct(repo_path="../outside.env", host_path=None)
        with pytest.raises(TestEnvFileError, match=r"escapes|contain"):
            load_test_env(cfg, repo_root=tmp_path / "repo")

    def test_absolute_repo_path_rejected(self, tmp_path: Path) -> None:
        cfg = TestEnvConfig.model_construct(repo_path="/etc/passwd", host_path=None)
        with pytest.raises(TestEnvFileError, match=r"escapes|relative|contain"):
            load_test_env(cfg, repo_root=tmp_path / "repo")


class TestMissingFileError:
    def test_missing_repo_file_names_path_and_field(self, tmp_path: Path) -> None:
        cfg = TestEnvConfig(repo_path=".env.test")
        with pytest.raises(TestEnvFileError) as exc:
            load_test_env(cfg, repo_root=tmp_path)
        msg = str(exc.value)
        assert ".env.test" in msg
        assert "repo_path" in msg

    def test_missing_host_file_names_path_and_field(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.env"
        cfg = TestEnvConfig(host_path=str(missing))
        with pytest.raises(TestEnvFileError) as exc:
            load_test_env(cfg, repo_root=tmp_path)
        msg = str(exc.value)
        assert str(missing) in msg
        assert "host_path" in msg


class TestPerformanceBudget:
    """SC-007: parsing a typical (<=50 entries) file completes in under 10 ms."""

    def test_fifty_entries_under_10ms(self, tmp_path: Path) -> None:
        lines = "\n".join(f"KEY_{i}=value_{i}" for i in range(50))
        _write(tmp_path / ".env.test", lines + "\n")
        cfg = TestEnvConfig(repo_path=".env.test")
        start = time.perf_counter()
        out = load_test_env(cfg, repo_root=tmp_path)
        elapsed = time.perf_counter() - start
        assert len(out) == 50
        assert elapsed < 0.010, f"loader took {elapsed * 1000:.2f}ms (budget 10ms)"
