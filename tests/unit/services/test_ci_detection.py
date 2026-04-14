"""Unit tests for CI command detection (043)."""
from __future__ import annotations

import json
from pathlib import Path

from coordinare.services.ci_detection import detect

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
        assert result.test_command is None
