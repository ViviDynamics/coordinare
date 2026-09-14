"""409: stack detection knew four stacks and probed the repo root only.

New detectors for Go, Rust, Gradle, Maven and .NET; Python detection that
works without pyproject.toml; Ruby that tells rspec from minitest; Node that
honours packageManager and lockfiles; Makefile ci+test; and monorepo
sub-root probing. Operator overrides live in test_409_overrides.py
(performer tree).
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from coordinare.services.ci_detection import detect


@pytest.fixture(autouse=True)
def _mock_verify_tool():
    with patch("coordinare_ci_detection._verify_tool", return_value=True):
        yield


class TestGoDetection:
    def test_go_mod(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module example.com/app\n\ngo 1.22\n")
        result = detect(tmp_path)
        assert result.stack == "go"
        assert result.test_command == "go test ./..."
        assert result.lint_command == "go vet ./..."

    def test_ruby_beats_go(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module example.com/app\n")
        (tmp_path / "Gemfile").write_text("gem 'x'\n")
        assert detect(tmp_path).stack == "ruby"


class TestRustDetection:
    def test_cargo_toml(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text("[package]\nname = \"app\"\n")
        result = detect(tmp_path)
        assert result.stack == "rust"
        assert result.test_command == "cargo test"

    def test_go_beats_rust(self, tmp_path: Path) -> None:
        (tmp_path / "Cargo.toml").write_text("[package]\nname = \"app\"\n")
        (tmp_path / "go.mod").write_text("module example.com/app\n")
        assert detect(tmp_path).stack == "go"


class TestGradleDetection:
    def test_gradle_wrapper(self, tmp_path: Path) -> None:
        (tmp_path / "build.gradle").write_text("plugins { id 'java' }\n")
        (tmp_path / "gradlew").write_text("#!/bin/sh\n")
        result = detect(tmp_path)
        assert result.stack == "gradle"
        assert result.test_command == "./gradlew test"

    def test_gradle_without_wrapper(self, tmp_path: Path) -> None:
        (tmp_path / "build.gradle.kts").write_text("plugins { }\n")
        result = detect(tmp_path)
        assert result.stack == "gradle"
        assert result.test_command == "gradle test"


class TestMavenDetection:
    def test_pom_xml(self, tmp_path: Path) -> None:
        (tmp_path / "pom.xml").write_text("<project/>\n")
        result = detect(tmp_path)
        assert result.stack == "maven"
        assert result.test_command == "mvn test"


class TestDotnetDetection:
    def test_csproj(self, tmp_path: Path) -> None:
        (tmp_path / "App.csproj").write_text("<Project Sdk=\"Microsoft.NET.Sdk\"/>\n")
        result = detect(tmp_path)
        assert result.stack == "dotnet"
        assert result.test_command == "dotnet test"

    def test_sln(self, tmp_path: Path) -> None:
        (tmp_path / "App.sln").write_text("")
        result = detect(tmp_path)
        assert result.stack == "dotnet"
        assert result.test_command == "dotnet test"


class TestPythonWithoutPyproject:
    def test_pytest_ini(self, tmp_path: Path) -> None:
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        result = detect(tmp_path)
        assert result.stack == "python"
        assert result.test_command == "python -m pytest"

    def test_tox_ini_is_pass_through(self, tmp_path: Path) -> None:
        """tox manages its own envs; pytest past it bypasses the declared
        environments, so the detector stays fail-safe with no command."""
        (tmp_path / "tox.ini").write_text("[tox]\nenvlist = py312\n")
        result = detect(tmp_path)
        assert result.stack == "python"
        assert result.test_command is None

    def test_noxfile_py_is_pass_through(self, tmp_path: Path) -> None:
        (tmp_path / "noxfile.py").write_text("import nox\n")
        assert detect(tmp_path).test_command is None

    def test_requirements_txt_with_tests_dir(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("django==4.2\n")
        (tmp_path / "tests").mkdir()
        result = detect(tmp_path)
        assert result.stack == "python"
        assert result.test_command == "python -m pytest"

    def test_django_manage_py(self, tmp_path: Path) -> None:
        (tmp_path / "manage.py").write_text("#!/usr/bin/env python\n")
        result = detect(tmp_path)
        assert result.stack == "python"
        assert result.test_command == "python manage.py test"

    def test_requirements_txt_without_tests_dir(self, tmp_path: Path) -> None:
        (tmp_path / "requirements.txt").write_text("flask==3.0\n")
        result = detect(tmp_path)
        assert result.stack == "python"
        assert result.test_command is None


class TestRubyMinitestVsRspec:
    def test_rubocop_with_spec_dir_is_rspec(self, tmp_path: Path) -> None:
        (tmp_path / "Gemfile").write_text("gem 'x'\n")
        (tmp_path / ".rubocop.yml").write_text("AllCops:\n")
        (tmp_path / "spec").mkdir()
        assert detect(tmp_path).test_command == "bundle exec rspec"

    def test_rubocop_with_rspec_file_is_rspec(self, tmp_path: Path) -> None:
        (tmp_path / "Gemfile").write_text("gem 'x'\n")
        (tmp_path / ".rubocop.yml").write_text("AllCops:\n")
        (tmp_path / ".rspec").write_text("")
        assert detect(tmp_path).test_command == "bundle exec rspec"

    def test_rubocop_with_test_dir_is_minitest(self, tmp_path: Path) -> None:
        """minitest apps must not be handed rspec: the command is not installed
        and every gate run exited 127."""
        (tmp_path / "Gemfile").write_text("gem 'rails'\n")
        (tmp_path / ".rubocop.yml").write_text("AllCops:\n")
        (tmp_path / "test").mkdir()
        result = detect(tmp_path)
        assert result.test_command == "bundle exec rake test"


class TestNodePackageManager:
    def _package(self, tmp_path: Path) -> None:
        (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run", "lint": "eslint ."}}))

    def test_package_manager_field_pnpm(self, tmp_path: Path) -> None:
        self._package(tmp_path)
        (tmp_path / "package.json").write_text(json.dumps({
            "packageManager": "pnpm@9.0.0",
            "scripts": {"test": "vitest run"},
        }))
        result = detect(tmp_path)
        assert result.test_command == "pnpm test"

    def test_pnpm_lockfile(self, tmp_path: Path) -> None:
        self._package(tmp_path)
        (tmp_path / "pnpm-lock.yaml").write_text("")
        assert detect(tmp_path).test_command == "pnpm test"

    def test_yarn_lockfile(self, tmp_path: Path) -> None:
        self._package(tmp_path)
        (tmp_path / "yarn.lock").write_text("")
        assert detect(tmp_path).test_command == "yarn test"
        assert detect(tmp_path).lint_command == "yarn run lint"

    def test_bun_lockfile(self, tmp_path: Path) -> None:
        self._package(tmp_path)
        (tmp_path / "bun.lockb").write_text("")
        assert detect(tmp_path).test_command == "bun run test"

    def test_bun_text_lockfile(self, tmp_path: Path) -> None:
        self._package(tmp_path)
        (tmp_path / "bun.lock").write_text("")
        assert detect(tmp_path).test_command == "bun run test"

    def test_no_lockfile_is_npm(self, tmp_path: Path) -> None:
        self._package(tmp_path)
        assert detect(tmp_path).test_command == "npm test"


class TestMakefileCiAndTest:
    def test_ci_and_test_targets_both_fire(self, tmp_path: Path) -> None:
        """A Makefile with both ci: and test: must still report make test;
        None silently disabled the local gate for the whole repo."""
        (tmp_path / "Makefile").write_text("ci:\n\tall\n\ntest:\n\tpytest\n")
        result = detect(tmp_path)
        assert result.stack == "make"
        assert result.lint_command == "make ci"
        assert result.test_command == "make test"


class TestMonorepoSubRoots:
    def test_per_root_commands_returned(self, tmp_path: Path) -> None:
        (tmp_path / "api").mkdir()
        (tmp_path / "web").mkdir()
        (tmp_path / "api" / "go.mod").write_text("module example.com/api\n")
        (tmp_path / "web" / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
        result = detect(tmp_path, sub_roots=("api", "web"))
        assert result.per_root is not None
        assert result.per_root["api"].test_command == "go test ./..."
        assert result.per_root["web"].test_command == "npm test"

    def test_missing_root_is_skipped(self, tmp_path: Path) -> None:
        (tmp_path / "api").mkdir()
        (tmp_path / "api" / "go.mod").write_text("module example.com/api\n")
        result = detect(tmp_path, sub_roots=("api", "does-not-exist"))
        assert result.per_root is not None
        assert set(result.per_root) == {"api"}

    def test_no_sub_roots_is_none(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module example.com/app\n")
        assert detect(tmp_path).per_root is None

    def test_all_roots_missing_is_empty_map(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module example.com/app\n")
        result = detect(tmp_path, sub_roots=("nowhere",))
        assert result.per_root == {}

    def test_unknown_root_still_detects_top_level(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module example.com/app\n")
        result = detect(tmp_path, sub_roots=())
        assert result.stack == "go"
        assert result.per_root is None


class TestPatternValidation:
    def test_malformed_pattern_fails_config_load(self) -> None:
        """A bad regex must fail config validate, not the implementer mid-turn."""
        from coordinare.config import LocalTestGateConfig

        with pytest.raises(ValueError):
            LocalTestGateConfig(test_path_patterns=["["])

    def test_valid_patterns_load(self) -> None:
        from coordinare.config import LocalTestGateConfig

        gate = LocalTestGateConfig(test_path_patterns=[r"\.feature$"])
        assert gate.test_path_patterns == [r"\.feature$"]
