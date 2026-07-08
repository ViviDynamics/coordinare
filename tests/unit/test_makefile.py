"""130: static guard test for the top-level Makefile.

The Makefile is a thin façade over the existing ``bin/`` scripts and ``.venv``
tools. This test asserts the invariants whose regression would actually hurt —
missing targets, a target that isn't ``.PHONY``, CI-parity drift, a test target
that drops the required env-unset, or a ``clean`` that deletes something it
shouldn't — by reading the Makefile text (no execution of docker/e2e needed).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

MAKEFILE = Path(__file__).resolve().parents[2] / "Makefile"

# FR-003: the full task surface that MUST be reachable via a make target.
REQUIRED_TARGETS = {
    "help",
    "lint", "fmt", "lint-fix",
    "test", "test-unit", "test-contract", "test-all",
    "build", "e2e", "docker", "ci", "build-all",
    "run", "start", "stop", "performer-logs",
    "install", "uninstall", "version", "validate-version",
    "clean",
}


@pytest.fixture(scope="module")
def text() -> str:
    assert MAKEFILE.is_file(), f"Makefile missing at {MAKEFILE}"
    return MAKEFILE.read_text()


def _defined_targets(text: str) -> set[str]:
    """Target names with a rule (``name:`` at line start, excluding assignments)."""
    names: set[str] = set()
    for line in text.splitlines():
        m = re.match(r"^([a-zA-Z0-9_.-]+)\s*:(?!=)", line)
        if m:
            names.add(m.group(1))
    return names


def _phony_targets(text: str) -> set[str]:
    """Everything listed across (possibly line-continued) ``.PHONY:`` decls."""
    names: set[str] = set()
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].startswith(".PHONY:"):
            block = lines[i][len(".PHONY:"):]
            while block.rstrip().endswith("\\"):
                block = block.rstrip()[:-1] + " "
                i += 1
                block += lines[i] if i < len(lines) else ""
            names.update(block.split())
        i += 1
    return names


def _recipe(text: str, target: str) -> str:
    """Return the (tab-indented) recipe body of a target, joined by newlines."""
    lines = text.splitlines()
    out: list[str] = []
    capturing = False
    pat = re.compile(rf"^{re.escape(target)}\s*:(?!=)")
    for line in lines:
        if pat.match(line):
            capturing = True
            continue
        if capturing:
            if line.startswith("\t"):
                out.append(line[1:])
            elif line.strip() == "":
                continue  # blank lines don't end a recipe
            else:
                break
    return "\n".join(out)


def test_makefile_exists(text: str) -> None:
    assert text.strip(), "Makefile is empty"


def test_default_goal_is_help(text: str) -> None:
    assert ".DEFAULT_GOAL := help" in text  # bare `make` prints help (FR-002)


def test_all_required_targets_defined(text: str) -> None:
    missing = REQUIRED_TARGETS - _defined_targets(text)
    assert not missing, f"Makefile missing targets (FR-003): {sorted(missing)}"


def test_all_targets_are_phony(text: str) -> None:
    # FR-005: every public target must be .PHONY (guards require-venv/require-env too).
    phony = _phony_targets(text)
    not_phony = REQUIRED_TARGETS - phony
    assert not not_phony, f"targets missing from .PHONY (FR-005): {sorted(not_phony)}"


def test_help_is_grouped(text: str) -> None:
    # FR-002: grouped help via ##@ section headers covering the six categories.
    groups = {m.lower() for m in re.findall(r"^##@\s*(\w+)", text, re.MULTILINE)}
    assert {"dev", "test", "build", "run", "release", "clean"} <= groups, groups


def test_every_public_target_has_a_description(text: str) -> None:
    # Each required target (except pure aliases) carries a `## ` help description.
    described = set(re.findall(r"^([a-zA-Z0-9_.-]+)\s*:.*##", text, re.MULTILINE))
    # aliases delegate to their primary and need no separate description
    aliases = {"lint-fix", "test", "build-all"}
    undescribed = (REQUIRED_TARGETS - aliases) - described
    assert not undescribed, f"targets lacking a `## ` help line: {sorted(undescribed)}"


def test_pytest_var_carries_the_required_env_unset(text: str) -> None:
    # FR-006 / SC-004: the env-unset lives in one PYTEST var used by all test targets.
    m = re.search(r"^PYTEST\s*:?=\s*(.+)$", text, re.MULTILINE)
    assert m, "PYTEST variable not defined"
    val = m.group(1)
    assert "-u COORDINARE_INFERENCE_MAX_TOKENS" in val
    assert "-u HERMES_CONTEXT_WINDOW" in val
    assert ".venv/bin/pytest" in val


@pytest.mark.parametrize("target", ["test-unit", "test-contract", "test-all"])
def test_test_targets_use_pytest_var(text: str, target: str) -> None:
    # FR-006: no test target may hand-roll pytest and drop the env-unset.
    assert "$(PYTEST)" in _recipe(text, target), f"{target} does not use $(PYTEST)"


def test_test_all_covers_unit_and_contract(text: str) -> None:
    recipe = _recipe(text, "test-all")
    assert "tests/unit" in recipe and "tests/contract" in recipe


def test_ci_is_exact_build_all_parity(text: str) -> None:
    # FR-007 / SC-003: `make ci` delegates to bin/build --all (no reimplementation).
    assert "bin/build --all" in _recipe(text, "ci")


def test_build_targets_delegate_to_bin_build(text: str) -> None:
    # FR-004: build family delegates to bin/build (single source of truth).
    assert _recipe(text, "build").strip() == "bin/build"
    assert "bin/build --e2e" in _recipe(text, "e2e")
    assert "bin/build --docker" in _recipe(text, "docker")


@pytest.mark.parametrize(
    "target,script",
    [
        ("stop", "bin/stop"),
        ("performer-logs", "bin/performer-logs"),
        ("install", "bin/install"),
        ("uninstall", "bin/uninstall"),
        ("version", "bin/update-version"),
        ("validate-version", "bin/validate-version"),
    ],
)
def test_operator_targets_delegate_to_bin(text: str, target: str, script: str) -> None:
    assert script in _recipe(text, target), f"{target} should delegate to {script}"


@pytest.mark.parametrize("target", ["run", "start"])
def test_launch_targets_guard_and_source_env(text: str, target: str) -> None:
    # FR-008: launch targets require .env (require-env prereq) and source it.
    header = re.search(rf"^{target}\s*:(.*)$", text, re.MULTILINE)
    assert header and "require-env" in header.group(1), f"{target} missing require-env prereq"
    recipe = _recipe(text, target)
    assert ". ./.env" in recipe, f"{target} does not source .env"


def test_require_env_refuses_when_missing(text: str) -> None:
    recipe = _recipe(text, "require-env")
    assert "test -f .env" in recipe and "exit 1" in recipe


def test_require_venv_is_actionable(text: str) -> None:
    recipe = _recipe(text, "require-venv")
    assert "test -x .venv/bin/python" in recipe and "exit 1" in recipe


def test_clean_only_touches_caches(text: str) -> None:
    # FR-010: clean removes cache detritus only — never source, venv, env, caches.
    recipe = _recipe(text, "clean")
    assert "__pycache__" in recipe and ".pytest_cache" in recipe and ".ruff_cache" in recipe
    # Inspect only DESTRUCTIVE lines (rm / -delete / -exec rm) — echo/comment
    # lines may legitimately mention .venv/.env (e.g. "…untouched").
    destructive = [
        ln for ln in recipe.splitlines()
        if re.search(r"\brm\b|-delete\b", ln)
    ]
    assert destructive, "clean has no destructive line?"
    for line in destructive:
        # Never touch the venv, env file, or env-caches on ANY destructive line.
        for danger in (".venv", "env-cache", ".env"):
            assert danger not in line, f"clean destructive line touches {danger!r}: {line}"
        if "find " in line:
            # A find that deletes must be constrained to a cache name pattern
            # (src/tests/agent here are search ROOTS, not deletion targets).
            assert "__pycache__" in line or "*.pyc" in line, f"unscoped find delete: {line}"
        else:
            # A bare `rm` must only name cache artifacts, never source/docs/specs.
            for danger in ("src", "tests", "agent", "docs", "specs"):
                assert danger not in line, f"clean rm targets {danger!r}: {line}"
