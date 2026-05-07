"""Unit tests for CI command detection (043)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinare.services.ci_detection import _verify_tool, detect


# 044: Mock _verify_tool to always return True in detection tests.
# These tests verify file-convention detection logic, not tool installation.
@pytest.fixture(autouse=True)
def _mock_verify_tool():
    with patch("coordinare.services.ci_detection._verify_tool", return_value=True):
        yield

# ---------------------------------------------------------------------------
# T010 — Ruby detection
# ---------------------------------------------------------------------------


class TestRubyDetection:
    def test_gemfile_with_rubocop(self, tmp_path: Path) -> None:
        (tmp_path / "Gemfile").write_text("gem 'rubocop'\n")
        (tmp_path / ".rubocop.yml").write_text("AllCops:\n  Enabled: true\n")
        result = detect(tmp_path)
        assert result.stack == "ruby"
        assert result.lint_command == "bundle exec rubocop"
        assert result.test_command == "bundle exec rspec"
        assert result.detected_from == ".rubocop.yml"

    def test_gemfile_with_rakefile_no_rubocop(self, tmp_path: Path) -> None:
        (tmp_path / "Gemfile").write_text("gem 'rails'\n")
        (tmp_path / "Rakefile").write_text("task :test\n")
        result = detect(tmp_path)
        assert result.stack == "ruby"
        assert result.lint_command is None
        assert result.test_command == "bundle exec rake test"
        assert result.detected_from == "Rakefile"

    def test_gemfile_only(self, tmp_path: Path) -> None:
        (tmp_path / "Gemfile").write_text("gem 'rails'\n")
        result = detect(tmp_path)
        assert result.stack == "ruby"
        assert result.lint_command is None
        assert result.test_command is None
        assert result.detected_from == "Gemfile"


# ---------------------------------------------------------------------------
# T011 — Python detection
# ---------------------------------------------------------------------------


class TestPythonDetection:
    def test_pyproject_with_ruff(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "foo"\n[tool.ruff]\nline-length = 88\n'
            '[project.optional-dependencies]\ndev = ["pytest", "ruff"]\n'
        )
        result = detect(tmp_path)
        assert result.stack == "python"
        assert result.lint_command == "ruff check ."
        assert result.test_command == "pytest"

    def test_pyproject_with_flake8(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "foo"\n'
            '[project.optional-dependencies]\ndev = ["pytest", "flake8"]\n'
        )
        result = detect(tmp_path)
        assert result.stack == "python"
        assert result.lint_command == "flake8"
        assert result.test_command == "pytest"

    def test_pyproject_minimal(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "foo"\n')
        result = detect(tmp_path)
        assert result.stack == "python"
        assert result.lint_command is None
        assert result.test_command is None


# ---------------------------------------------------------------------------
# T012 — Node detection
# ---------------------------------------------------------------------------


class TestNodeDetection:
    def test_package_json_with_lint_and_test(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({
            "scripts": {"lint": "eslint .", "test": "jest"},
        }))
        result = detect(tmp_path)
        assert result.stack == "node"
        assert result.lint_command == "npm run lint"
        assert result.test_command == "npm test"

    def test_package_json_test_only(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({
            "scripts": {"test": "jest"},
        }))
        result = detect(tmp_path)
        assert result.stack == "node"
        assert result.lint_command is None
        assert result.test_command == "npm test"

    def test_package_json_no_scripts(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({"name": "foo"}))
        result = detect(tmp_path)
        assert result.stack == "node"
        assert result.lint_command is None
        assert result.test_command is None

    def test_package_json_malformed(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text("not json {{{")
        result = detect(tmp_path)
        assert result.stack == "node"
        assert result.lint_command is None
        assert result.test_command is None


# ---------------------------------------------------------------------------
# T013 — Makefile detection
# ---------------------------------------------------------------------------


class TestMakefileDetection:
    def test_makefile_with_lint_and_test(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("lint:\n\truff check .\n\ntest:\n\tpytest\n")
        result = detect(tmp_path)
        assert result.stack == "make"
        assert result.lint_command == "make lint"
        assert result.test_command == "make test"

    def test_makefile_with_ci_target(self, tmp_path: Path) -> None:
        (tmp_path / "Makefile").write_text("ci:\n\truff check . && pytest\n")
        result = detect(tmp_path)
        assert result.stack == "make"
        assert result.lint_command == "make ci"
        assert result.test_command is None

    def test_makefile_no_relevant_targets(self, tmp_path: Path) -> None:
        """Makefile exists but has no lint/test/ci targets → not detected."""
        (tmp_path / "Makefile").write_text("build:\n\tgcc main.c\n")
        result = detect(tmp_path)
        assert result.stack == "unknown"


# ---------------------------------------------------------------------------
# T014 — Unknown stack
# ---------------------------------------------------------------------------


class TestUnknownStack:
    def test_empty_directory(self, tmp_path: Path) -> None:
        result = detect(tmp_path)
        assert result.stack == "unknown"
        assert result.lint_command is None
        assert result.test_command is None
        assert result.detected_from == ""

    def test_unrecognised_files(self, tmp_path: Path) -> None:
        (tmp_path / "main.go").write_text("package main\n")
        result = detect(tmp_path)
        assert result.stack == "unknown"


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------


class TestDetectionPriority:
    def test_ruby_takes_priority_over_node(self, tmp_path: Path) -> None:
        """When both Gemfile and package.json exist, Ruby wins."""
        (tmp_path / "Gemfile").write_text("gem 'rails'\n")
        (tmp_path / ".rubocop.yml").write_text("")
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
        result = detect(tmp_path)
        assert result.stack == "ruby"

    def test_python_takes_priority_over_make(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[tool.ruff]\n')
        (tmp_path / "Makefile").write_text("lint:\n\truff check .\n")
        result = detect(tmp_path)
        assert result.stack == "python"


class TestNodeNonDictJson:
    def test_package_json_array_content(self, tmp_path: Path) -> None:
        """package.json containing a JSON array instead of object."""
        (tmp_path / "package.json").write_text("[]")
        result = detect(tmp_path)
        assert result.stack == "node"
        assert result.lint_command is None


# ---------------------------------------------------------------------------
# Tool verification (044)
# ---------------------------------------------------------------------------


class TestToolVerification:
    def test_verify_tool_returns_false_nulls_lint_command(self, tmp_path: Path) -> None:
        """When _verify_tool returns False, lint_command is set to None."""
        (tmp_path / "pyproject.toml").write_text('[tool.ruff]\nname = "foo"\n[project.optional-dependencies]\ndev = ["pytest"]\n')
        with patch("coordinare.services.ci_detection._verify_tool", return_value=False):
            result = detect(tmp_path)
        assert result.stack == "python"
        assert result.lint_command is None
        assert result.test_command == "pytest"

    def test_verify_tool_returncode_zero_keeps_lint_command(self, tmp_path: Path) -> None:
        """When _verify_tool returns True, lint_command is preserved."""
        (tmp_path / "pyproject.toml").write_text('[tool.ruff]\nname = "foo"\n[project.optional-dependencies]\ndev = ["pytest"]\n')
        with patch("coordinare.services.ci_detection._verify_tool", return_value=True):
            result = detect(tmp_path)
        assert result.stack == "python"
        assert result.lint_command == "ruff check ."
        assert result.test_command == "pytest"


# ---------------------------------------------------------------------------
# _verify_tool implementation tests
# ---------------------------------------------------------------------------


class TestVerifyToolImpl:
    def test_verify_tool_finds_available_tool(self, tmp_path: Path) -> None:
        """Test that _verify_tool returns True for an available tool."""
        # Use a tool that should be available in the test environment
        result = _verify_tool("python --version", tmp_path)
        assert result is True

    def test_verify_tool_missing_tool_returns_false(self, tmp_path: Path) -> None:
        """Test that _verify_tool returns False for a missing tool."""
        result = _verify_tool("nonexistent-tool-xyz-not-installed", tmp_path)
        assert result is False

    def test_verify_tool_bundle_command(self, tmp_path: Path, monkeypatch) -> None:
        """Test that _verify_tool handles bundle commands correctly."""
        import subprocess

        # Mock subprocess.run to simulate bundle exec command
        original_run = subprocess.run
        calls = []

        def mock_run(*args, **kwargs):
            calls.append(args[0])
            # Simulate bundle exec tool --version
            if "bundle" in args[0] and "exec" in args[0]:
                return subprocess.CompletedProcess(args[0], returncode=0)
            return original_run(*args, **kwargs)

        monkeypatch.setattr(subprocess, "run", mock_run)
        _verify_tool("bundle exec rubocop", tmp_path)
        # Should construct: ["bundle", "exec", "rubocop", "--version"]
        assert len(calls) > 0
        assert calls[0][0] == "bundle"

    def test_verify_tool_timeout_returns_false(self, tmp_path: Path, monkeypatch) -> None:
        """Test that _verify_tool returns False on timeout."""
        import subprocess

        def mock_run(*args, **kwargs):
            raise subprocess.TimeoutExpired("cmd", timeout=5)

        monkeypatch.setattr(subprocess, "run", mock_run)
        result = _verify_tool("slow-tool", tmp_path)
        assert result is False

    def test_verify_tool_oserror_returns_false(self, tmp_path: Path, monkeypatch) -> None:
        """Test that _verify_tool returns False on OSError."""
        import subprocess

        def mock_run(*args, **kwargs):
            raise OSError("Permission denied")

        monkeypatch.setattr(subprocess, "run", mock_run)
        result = _verify_tool("restricted-tool", tmp_path)
        assert result is False

    def test_verify_tool_filenotfound_returns_false(self, tmp_path: Path, monkeypatch) -> None:
        """Test that _verify_tool returns False on FileNotFoundError."""
        import subprocess

        def mock_run(*args, **kwargs):
            raise FileNotFoundError("Tool not found")

        monkeypatch.setattr(subprocess, "run", mock_run)
        result = _verify_tool("missing-tool", tmp_path)
        assert result is False

    def test_verify_tool_empty_command_returns_false(self, tmp_path: Path) -> None:
        """Test that _verify_tool returns False for empty command."""
        result = _verify_tool("", tmp_path)
        assert result is False

    def test_verify_tool_extracts_non_bundle_binary(self, tmp_path: Path, monkeypatch) -> None:
        """Test that _verify_tool extracts just the binary for non-bundle commands."""
        import subprocess

        calls = []

        def mock_run(*args, **kwargs):
            calls.append(args[0])
            return subprocess.CompletedProcess(args[0], returncode=0)

        monkeypatch.setattr(subprocess, "run", mock_run)
        result = _verify_tool("ruff check .", tmp_path)
        # Should construct: ["ruff", "--version"] (drop "check" and ".")
        assert result is True
        assert len(calls) > 0
        assert calls[0] == ["ruff", "--version"]
