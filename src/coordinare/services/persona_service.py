"""Persona service for per-role behavioral instructions (018-performer-personas)."""
from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from typing import TYPE_CHECKING, Any

import structlog
import yaml

from coordinare.config import PERSONA_MAX_LENGTH

if TYPE_CHECKING:
    from pathlib import Path

    from coordinare.config import PersonasConfig

_logger = structlog.get_logger(__name__)

DEFAULT_INSTRUCTIONS: dict[str, str] = {
    "advocate": (
        "Scan GitHub issues and identify the highest-value, well-specified items "
        "ready for implementation. Prioritise issues with clear acceptance criteria, "
        "defined scope, and no unresolved blockers. Add matching issues to the project "
        "board and label them appropriately."
    ),
    "assessor": (
        "You are a pragmatic product manager evaluating whether a card is ready "
        "for a developer to pick up. Focus on user-facing intent and desired outcome — "
        "NOT implementation details like routes, file paths, or component lists. "
        "The developer has full access to the codebase and can discover technical "
        "specifics independently. Only block a card if the business goal or user "
        "intent is genuinely unclear. Err on the side of approving. Ask at most "
        "1-2 questions, and only when the desired outcome itself is ambiguous.\n"
        "When done, output your assessment as a JSON object with these fields:\n"
        '{"sufficient": true/false, "questions": ["question 1", "question 2"]}\n'
        "Your FINAL output MUST be valid JSON."
    ),
    "architect": (
        "Analyse the codebase and produce a structured technical plan for implementing "
        "the card. Identify affected files, required changes, data model impacts, and "
        "potential risks. Commit the plan to docs/coordinare-architecture.md on the "
        "feature branch before implementation begins."
    ),
    "implementer": (
        "Implement the card according to the acceptance criteria and architecture plan. "
        "Write clean code that follows the project's existing conventions and patterns. "
        "Write unit tests and integration/feature tests covering all new functionality. "
        "Before pushing, run the full test suite locally and fix any failures — "
        "do NOT push code that breaks existing tests or leaves new code untested. "
        "Open a pull request with a clear description referencing the issue when done.\n\n"
        "When addressing review feedback:\n"
        "1. Read ALL comments carefully and identify the PATTERN behind them — reviewers "
        "often tag a few examples but expect you to fix every similar instance across the "
        "entire codebase, not just the lines they commented on.\n"
        "2. Search the full codebase for all occurrences of the same issue pattern and fix "
        "them all. For example, if a reviewer flags one linter disable, remove ALL linter "
        "disables in your changes and refactor the code properly instead.\n"
        "3. Never resolve or dismiss review threads — only the human reviewer does that.\n"
        "4. Do not mark feedback as addressed unless you actually changed the code."
    ),
    "reviewer": (
        "You are a meticulous senior code reviewer. Your job is to catch real problems "
        "before they reach production. Read EVERY changed file and EVERY changed line.\n\n"
        "## Review process\n"
        "1. Read the PR diff thoroughly — understand what changed and why.\n"
        "2. Read surrounding code in unchanged files to understand existing patterns, "
        "naming conventions, architecture, and idioms used in this project.\n"
        "3. Compare the new code against the existing codebase conventions.\n"
        "4. Check that tests exist and meaningfully cover the new behavior.\n\n"
        "## What to flag as issues (put in 'comments')\n"
        "Reference the specific file and line for each issue.\n\n"
        "**Linter / style disables**: Flag EVERY instance where the developer disabled "
        "a linter rule (RuboCop, ESLint, pylint, etc.) instead of fixing the underlying "
        "code. The right fix is almost always to refactor, not to silence the tool.\n\n"
        "**Convention violations**: Flag code that breaks patterns established elsewhere "
        "in the codebase — naming style, file organization, method signatures, error "
        "handling approach, test structure, module boundaries. Consistency matters.\n\n"
        "**Readability**: Flag methods longer than ~20 lines, nesting deeper than 3 "
        "levels, unclear variable/method names, magic numbers/strings without constants, "
        "and complex logic missing a brief explanatory comment.\n\n"
        "**Correctness**: Flag logic errors, unhandled edge cases (nil/null, empty "
        "collections, boundary values), race conditions, missing validations, and "
        "incorrect assumptions about data shape or availability.\n\n"
        "**Test quality**: Flag missing tests for new public methods or behavior, tests "
        "that only cover the happy path, brittle assertions (testing implementation "
        "rather than behavior), and missing edge case coverage.\n\n"
        "**Security**: Flag SQL injection, XSS, CSRF gaps, hardcoded secrets, overly "
        "broad permissions, and unvalidated user input.\n\n"
        "**Dead code & duplication**: Flag unused variables, unreachable branches, "
        "commented-out code left behind, and logic duplicated from existing helpers.\n\n"
        "## Approval criteria\n"
        "Do NOT approve if there are any linter disables, convention violations, or "
        "correctness issues. Only approve when the code is genuinely ready for a human "
        "reviewer to glance at and merge. When in doubt, request changes.\n\n"
        "When done, output your review as a JSON object with these fields:\n"
        '{"approved": true/false, "body": "overall summary", '
        '"comments": ["path/to/file:42 — description of issue", ...], '
        '"suggestions": ["non-blocking improvement 1", ...]}\n'
        "Your FINAL output MUST be valid JSON."
    ),
    "security": (
        "Analyse the changes for OWASP Top 10 vulnerabilities, secret leakage, and "
        "insecure patterns. "
        "When done, output your findings as a JSON object with these fields:\n"
        '{"passed": true/false, "findings": [{"severity": "critical|high|medium|low", '
        '"category": "...", "description": "...", "file": "...", "routing": "implementer|architect"}]}\n'
        "Block on critical and high severity findings. Post advisory "
        "comments for medium and low severity findings. Your FINAL output MUST be valid JSON."
    ),
    "qa": (
        "Validate that the implementation satisfies every acceptance criterion by running "
        "the test suite and writing new tests for uncovered paths. "
        "When done, output your QA report as a JSON object with these fields:\n"
        '{"passed": true/false, "criteria_checked": 5, "criteria_passed": 4, '
        '"failures": [{"criterion": "...", "expected": "...", "actual": "...", "test": "..."}], '
        '"new_tests_added": ["path/to/test1.rb"]}\n'
        "Your FINAL output MUST be valid JSON."
    ),
    "tech_writer": (
        "Produce a CHANGELOG entry, update README sections affected by the change, and "
        "add inline documentation for public interfaces. Commit the documentation to the "
        "feature branch."
    ),
}

VALID_ROLES: frozenset[str] = frozenset(DEFAULT_INSTRUCTIONS.keys())


def get_effective_instructions(role: str, personas: PersonasConfig) -> str:
    """Return the active persona instructions for a role.

    Falls back to DEFAULT_INSTRUCTIONS[role] when the configured instructions
    are absent, empty, or whitespace-only.
    """
    if role not in VALID_ROLES:
        msg = f"Unknown role: {role!r}"
        raise ValueError(msg)
    from coordinare.config import PersonaConfig
    persona: PersonaConfig = getattr(personas, role)
    raw = persona.instructions or ""
    if raw.strip():
        return raw
    return DEFAULT_INSTRUCTIONS[role]


def save_persona(role: str, instructions: str, config_path: Path) -> None:
    """Write updated persona instructions to the YAML config file.

    Reads the existing config, sets personas.<role>.instructions, and writes
    it back atomically.

    Raises ValueError for: unknown roles, oversized instructions, missing
    config file, non-mapping YAML root, non-mapping ``personas`` section,
    or non-mapping role section.  Raises OSError on file I/O failures.

    Note: ``yaml.safe_load`` / ``yaml.safe_dump`` round-trips strip all YAML
    comments from the file.  This is a known limitation — any hand-written
    comments in ``config.yaml`` will be lost on the first ``save_persona``
    call.  Users who rely on comments should keep a separate backup or manage
    the file manually.

    Concurrency: the read-modify-write is atomic at the filesystem level
    (``os.replace``), but two overlapping calls can still lose each other's
    edits.  This is acceptable for the single-process daemon; add an OS-level
    lock (``fcntl.flock``) if multi-worker deployments become a requirement.
    """
    if role not in VALID_ROLES:
        msg = f"Unknown role: {role!r}"
        raise ValueError(msg)

    # Validate length on the effective (stripped) content, but persist the
    # original string so intentional leading/trailing whitespace is preserved.
    if len(instructions.strip()) > PERSONA_MAX_LENGTH:
        msg = f"Instructions exceed maximum length ({PERSONA_MAX_LENGTH} chars)"
        raise ValueError(msg)

    if not config_path.exists():
        msg = f"Config file does not exist: {config_path}"
        raise ValueError(msg)

    loaded = yaml.safe_load(config_path.read_text())
    if not isinstance(loaded, dict):
        msg = "Config file does not contain a YAML mapping"
        raise ValueError(msg)

    raw: dict[str, Any] = loaded

    personas_section = raw.setdefault("personas", {})
    if not isinstance(personas_section, dict):
        msg = "Config file 'personas' section is not a mapping"
        raise ValueError(msg)

    role_section = personas_section.setdefault(role, {})
    if not isinstance(role_section, dict):
        msg = f"Config file 'personas.{role}' section is not a mapping"
        raise ValueError(msg)

    role_section["instructions"] = instructions

    # Atomic write: write to a temp file in the same directory then rename so a
    # crash or disk-full error cannot leave a partially-written config file.
    # Preserve the original file mode so the replace does not change permissions.
    original_mode = stat.S_IMODE(os.stat(config_path).st_mode)

    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=config_path.parent, prefix=".coordinare_config_", suffix=".yaml.tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, original_mode)
        os.replace(tmp_path, str(config_path))
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def reset_persona(role: str, config_path: Path) -> None:
    """Clear persona instructions for a role, restoring built-in defaults."""
    save_persona(role, "", config_path)


def load_personas_hot(config_path: Any, fallback_config: Any) -> PersonasConfig:
    """Return the effective PersonasConfig, hot-reloading from *config_path* when possible.

    On every call, tries to parse ``config_path`` from disk so that dashboard
    and manual YAML edits take effect without a daemon restart.  If the file
    cannot be read or fails validation, falls back to ``fallback_config.personas``
    (if available) or a default ``PersonasConfig()``, and logs a warning with
    full exception details so operators can diagnose misconfigured YAML.
    """
    from pathlib import Path

    from coordinare.config import PersonasConfig

    if config_path is not None:
        p = config_path if isinstance(config_path, Path) else Path(config_path)
        if p.is_file():
            try:
                from coordinare.config import ProjectConfiguration

                return ProjectConfiguration.from_yaml(p).personas
            except Exception as exc:
                _logger.warning(
                    "persona_hot_reload_failed",
                    config_path=str(config_path),
                    error=str(exc),
                    error_type=type(exc).__name__,
                    exc_info=True,
                )

    if fallback_config is not None and hasattr(fallback_config, "personas"):
        return fallback_config.personas  # type: ignore[no-any-return]
    return PersonasConfig()
