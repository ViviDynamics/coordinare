"""Convention-based CI command detection for performer workspaces (043).

Inspects a workspace directory for well-known CI config files (Gemfile,
pyproject.toml, package.json, Makefile) and returns the lint and test
commands appropriate for the detected stack.  Used by:
  - Performer-side pre-commit checks (main.py)
  - Coordinare-side CI lint gate (monitor_performer._advance_stage)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class CIDetectionResult:
    """Result of convention-based CI command detection."""

    lint_command: str | None = None
    test_command: str | None = None
    stack: str = "unknown"
    detected_from: str = ""
    # 409: per-root results for a monorepo, keyed by the root path the caller
    # declared. None when no sub-roots were probed; an empty dict when every
    # declared root was skipped (absent or unmatched).
    per_root: dict[str, CIDetectionResult] | None = None


# ---------------------------------------------------------------------------
# 044: Tool verification — check tool is installed before returning command
# ---------------------------------------------------------------------------


def _verify_tool(command: str, cwd: Path) -> bool:
    """Check that the first word of *command* is an executable tool.

    Runs ``{tool} --version`` with a 5s timeout.  Returns True if it
    exits 0, False otherwise.  Used to avoid returning lint commands
    for tools that aren't installed (which would produce confusing
    errors and stdout contamination).
    """
    import shlex
    import subprocess

    parts = shlex.split(command)
    if not parts:
        return False
    # Extract just the tool binary for --version check.
    # "bundle exec rubocop" → ["bundle", "exec", "rubocop", "--version"]
    # "ruff check ." → ["ruff", "--version"] (drop subcommand args)
    # "npm run lint" → ["npm", "--version"]
    if parts[0] == "bundle" and len(parts) >= 3:
        # Keep "bundle exec <tool>" for Ruby tools
        verify_cmd = [*parts[:3], "--version"]
    else:
        # Just the first word (the binary itself)
        verify_cmd = [parts[0], "--version"]
    try:
        result = subprocess.run(
            verify_cmd,
            cwd=str(cwd),
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return False


# ---------------------------------------------------------------------------
# Detection logic — first match wins (priority order)
# ---------------------------------------------------------------------------


def _detect_ruby(workspace: Path) -> CIDetectionResult | None:
    """Detect Ruby project via Gemfile."""
    gemfile = workspace / "Gemfile"
    if not gemfile.is_file():
        return None
    rubocop = workspace / ".rubocop.yml"
    if rubocop.is_file():
        # 409: .rubocop.yml alone used to hardcode rspec, so a minitest app
        # (no spec/, no .rspec) got "bundle exec rspec", which is not
        # installed there: exit 127 on every gate run. Probe the convention
        # that actually declares a runner.
        if (workspace / ".rspec").is_file() or (workspace / "spec").is_dir():
            return CIDetectionResult(
                lint_command="bundle exec rubocop",
                test_command="bundle exec rspec",
                stack="ruby",
                detected_from=".rubocop.yml",
            )
        if (workspace / "test").is_dir():
            return CIDetectionResult(
                lint_command="bundle exec rubocop",
                test_command="bundle exec rake test",
                stack="ruby",
                detected_from=".rubocop.yml",
            )
        return CIDetectionResult(
            lint_command="bundle exec rubocop",
            test_command=None,
            stack="ruby",
            detected_from=".rubocop.yml",
        )
    rakefile = workspace / "Rakefile"
    if rakefile.is_file():
        return CIDetectionResult(
            lint_command=None,
            test_command="bundle exec rake test",
            stack="ruby",
            detected_from="Rakefile",
        )
    return CIDetectionResult(
        lint_command=None,
        test_command=None,
        stack="ruby",
        detected_from="Gemfile",
    )


def _detect_python(workspace: Path) -> CIDetectionResult | None:
    """Detect Python projects (409: no longer pyproject.toml-only).

    Django, tox, nox, setup.py and requirements.txt repositories were
    invisible to the pyproject-only probe, so their local test gate silently
    disabled itself.
    """
    pyproject = workspace / "pyproject.toml"
    if pyproject.is_file():
        content = pyproject.read_text(encoding="utf-8", errors="replace")
        has_ruff = "ruff" in content
        has_flake8 = "flake8" in content
        has_pytest = "pytest" in content
        lint = None
        if has_ruff:
            lint = "ruff check ."
        elif has_flake8:
            lint = "flake8"
        test = "pytest" if has_pytest else None
        return CIDetectionResult(
            lint_command=lint,
            test_command=test,
            stack="python",
            detected_from="pyproject.toml",
        )
    # No pyproject.toml: fall through the other Python conventions.
    manage = workspace / "manage.py"
    if manage.is_file():
        return CIDetectionResult(
            lint_command=None,
            test_command="python manage.py test",
            stack="python",
            detected_from="manage.py",
        )
    for marker in ("pytest.ini", "conftest.py"):
        if (workspace / marker).is_file():
            return CIDetectionResult(
                lint_command=None,
                test_command="python -m pytest",
                stack="python",
                detected_from=marker,
            )
    # tox/nox manage their own virtualenvs and sessions; running pytest past
    # them bypasses the environments the repository declares, so these two are
    # lint-only: a stack match, no test command, and the gate passes through
    # to remote CI rather than running the wrong tool.
    for marker in ("tox.ini", "noxfile.py"):
        if (workspace / marker).is_file():
            return CIDetectionResult(
                lint_command=None,
                test_command=None,
                stack="python",
                detected_from=marker,
            )
    # A plain setup.py or requirements.txt repository is Python only when it
    # has somewhere for the runner to collect: a tests/ directory.
    has_setup = (workspace / "setup.py").is_file() or (workspace / "requirements.txt").is_file()
    if has_setup and (workspace / "tests").is_dir():
        return CIDetectionResult(
            lint_command=None,
            test_command="python -m pytest",
            stack="python",
            detected_from="tests/",
        )
    if has_setup:
        return CIDetectionResult(
            lint_command=None,
            test_command=None,
            stack="python",
            detected_from="setup.py" if (workspace / "setup.py").is_file() else "requirements.txt",
        )
    return None


def _node_package_manager(workspace: Path, data: dict[str, Any]) -> str:
    """409: the runner is not always npm. packageManager pins it; lockfiles
    name it; npm remains the fallback."""
    package_manager = data.get("packageManager")
    if isinstance(package_manager, str):
        name = package_manager.split("@")[0].strip()
        if name in ("pnpm", "yarn", "bun", "npm"):
            return name
    if (workspace / "pnpm-lock.yaml").is_file():
        return "pnpm"
    if (workspace / "yarn.lock").is_file():
        return "yarn"
    if (workspace / "bun.lockb").is_file() or (workspace / "bun.lock").is_file():
        return "bun"
    return "npm"


def _detect_node(workspace: Path) -> CIDetectionResult | None:
    """Detect Node.js project via package.json."""
    pkg_json = workspace / "package.json"
    if not pkg_json.is_file():
        return None
    try:
        data = json.loads(
            pkg_json.read_text(encoding="utf-8", errors="replace"),
        )
    except (json.JSONDecodeError, OSError):
        return CIDetectionResult(
            lint_command=None, test_command=None,
            stack="node", detected_from="package.json",
        )
    if not isinstance(data, dict):
        return CIDetectionResult(
            lint_command=None, test_command=None,
            stack="node", detected_from="package.json",
        )
    raw_scripts = data.get("scripts")
    scripts = raw_scripts if isinstance(raw_scripts, dict) else {}
    pm = _node_package_manager(workspace, data)
    lint = f"{pm} run lint" if "lint" in scripts else None
    # bun test is Bun's built-in runner and would bypass scripts.test;
    # bun run test executes the declared script like the other managers.
    test = ("bun run test" if pm == "bun" else f"{pm} test") if "test" in scripts else None
    return CIDetectionResult(
        lint_command=lint,
        test_command=test,
        stack="node",
        detected_from="package.json",
    )


def _detect_go(workspace: Path) -> CIDetectionResult | None:
    """Detect a Go project via go.mod (409)."""
    if not (workspace / "go.mod").is_file():
        return None
    return CIDetectionResult(
        lint_command="go vet ./...",
        test_command="go test ./...",
        stack="go",
        detected_from="go.mod",
    )


def _detect_rust(workspace: Path) -> CIDetectionResult | None:
    """Detect a Rust project via Cargo.toml (409)."""
    if not (workspace / "Cargo.toml").is_file():
        return None
    return CIDetectionResult(
        lint_command=None,
        test_command="cargo test",
        stack="rust",
        detected_from="Cargo.toml",
    )


def _detect_gradle(workspace: Path) -> CIDetectionResult | None:
    """Detect a JVM project using Gradle (409). The wrapper wins when it is
    present: the pinned version is the one the repo CI runs."""
    for name in ("build.gradle", "build.gradle.kts"):
        if (workspace / name).is_file():
            gradlew = workspace / "gradlew"
            command = "./gradlew test" if gradlew.is_file() else "gradle test"
            return CIDetectionResult(
                lint_command=None,
                test_command=command,
                stack="gradle",
                detected_from=name,
            )
    return None


def _detect_maven(workspace: Path) -> CIDetectionResult | None:
    """Detect a JVM project using Maven (409)."""
    if not (workspace / "pom.xml").is_file():
        return None
    return CIDetectionResult(
        lint_command=None,
        test_command="mvn test",
        stack="maven",
        detected_from="pom.xml",
    )


def _detect_dotnet(workspace: Path) -> CIDetectionResult | None:
    """Detect a .NET project via csproj or sln (409)."""
    for pattern in ("*.csproj", "*.sln"):
        if any(workspace.glob(pattern)):
            return CIDetectionResult(
                lint_command=None,
                test_command="dotnet test",
                stack="dotnet",
                detected_from=pattern,
            )
    return None


def _detect_make(workspace: Path) -> CIDetectionResult | None:
    """Detect Makefile-based project."""
    makefile = workspace / "Makefile"
    if not makefile.is_file():
        return None
    content = makefile.read_text(encoding="utf-8", errors="replace")
    # Look for target declarations (lines starting with "target:")
    has_ci = "\nci:" in content or content.startswith("ci:")
    has_lint = "\nlint:" in content or content.startswith("lint:")
    has_test = "\ntest:" in content or content.startswith("test:")
    if has_ci:
        # 409: ci: plus test: used to set test_command=None, silently
        # disabling the local test gate for the repository. Both targets
        # exist, so both commands are reported.
        return CIDetectionResult(
            lint_command="make ci",
            test_command="make test" if has_test else None,
            stack="make",
            detected_from="Makefile",
        )
    lint = "make lint" if has_lint else None
    test = "make test" if has_test else None
    if lint or test:
        return CIDetectionResult(
            lint_command=lint,
            test_command=test,
            stack="make",
            detected_from="Makefile",
        )
    return None


def detect(workspace_path: Path, sub_roots: tuple[str, ...] | list[str] = ()) -> CIDetectionResult:
    """Detect CI commands for a workspace using file conventions.

    Checks in priority order: Ruby → Python → Node → Go → Rust → Gradle →
    Maven → .NET → Make.  Returns a result with ``stack="unknown"`` if no
    convention matches.

    When *sub_roots* is non-empty, each declared root that exists is probed
    with the same chain and the results ride the returned ``per_root`` map,
    keyed by the declared root name. A monorepo caller picks the root whose
    command it needs; the top-level fields stay the workspace root's own
    detection.
    """
    detectors = (
        _detect_ruby, _detect_python, _detect_node, _detect_go, _detect_rust,
        _detect_gradle, _detect_maven, _detect_dotnet, _detect_make,
    )
    per_root: dict[str, CIDetectionResult] | None = None
    if sub_roots:
        per_root = {}
        for root in sub_roots:
            root_path = workspace_path / root
            if not root_path.is_dir():
                continue
            for detector in detectors:
                result = detector(root_path)
                if result is not None:
                    # Same 044 lint verification the workspace root gets: an
                    # unavailable linter is nulled rather than composed into
                    # a cd-prefixed command the workflow would run blindly.
                    lint_cmd = result.lint_command
                    if lint_cmd and result.stack != "make" and not _verify_tool(
                        lint_cmd, root_path,
                    ):
                        logger.warning(
                            "ci_detection.tool_not_available",
                            lint_command=lint_cmd,
                            stack=result.stack,
                            workspace=str(root_path),
                        )
                        result = CIDetectionResult(
                            lint_command=None,
                            test_command=result.test_command,
                            stack=result.stack,
                            detected_from=result.detected_from,
                        )
                    per_root[root] = result
                    break
    for detector in detectors:
        result = detector(workspace_path)
        if result is not None:
            # 044: Verify lint tool is installed before returning the command.
            # Skip verification for make targets (no --version support).
            lint_cmd = result.lint_command
            if lint_cmd and result.stack != "make" and not _verify_tool(lint_cmd, workspace_path):
                    logger.warning(
                        "ci_detection.tool_not_available",
                        lint_command=lint_cmd,
                        stack=result.stack,
                        workspace=str(workspace_path),
                    )
                    result = CIDetectionResult(
                        lint_command=None,
                        test_command=result.test_command,
                        stack=result.stack,
                        detected_from=result.detected_from,
                    )
            logger.info(
                "ci_detection.detected",
                stack=result.stack,
                lint_command=result.lint_command,
                test_command=result.test_command,
                detected_from=result.detected_from,
                workspace=str(workspace_path),
            )
            return (
                result
                if per_root is None
                else CIDetectionResult(
                    lint_command=result.lint_command,
                    test_command=result.test_command,
                    stack=result.stack,
                    detected_from=result.detected_from,
                    per_root=per_root,
                )
            )
    logger.warning(
        "ci_detection.no_match",
        workspace=str(workspace_path),
    )
    if per_root is not None:
        return CIDetectionResult(per_root=per_root)
    return CIDetectionResult()
