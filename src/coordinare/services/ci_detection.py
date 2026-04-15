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
from typing import TYPE_CHECKING

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
        return CIDetectionResult(
            lint_command="bundle exec rubocop",
            test_command="bundle exec rspec",
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
    """Detect Python project via pyproject.toml."""
    pyproject = workspace / "pyproject.toml"
    if not pyproject.is_file():
        return None
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
    lint = "npm run lint" if "lint" in scripts else None
    test = "npm test" if "test" in scripts else None
    return CIDetectionResult(
        lint_command=lint,
        test_command=test,
        stack="node",
        detected_from="package.json",
    )


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
        return CIDetectionResult(
            lint_command="make ci",
            test_command=None,
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


def detect(workspace_path: Path) -> CIDetectionResult:
    """Detect CI commands for a workspace using file conventions.

    Checks in priority order: Ruby → Python → Node → Make.
    Returns a result with ``stack="unknown"`` if no convention matches.
    """
    for detector in (_detect_ruby, _detect_python, _detect_node, _detect_make):
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
            return result
    logger.warning(
        "ci_detection.no_match",
        workspace=str(workspace_path),
    )
    return CIDetectionResult()
