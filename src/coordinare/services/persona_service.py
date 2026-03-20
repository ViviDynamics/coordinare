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
        "Evaluate whether the card has sufficient context for implementation. "
        "Check for: clear requirements, defined scope, affected components, acceptance "
        "criteria, and technical constraints. Ask 3-5 specific, concrete questions when "
        "critical information is missing. Do not ask generic questions."
    ),
    "architect": (
        "Analyse the codebase and produce a structured technical plan for implementing "
        "the card. Identify affected files, required changes, data model impacts, and "
        "potential risks. Commit the plan to docs/coordinare-architecture.md on the "
        "feature branch before implementation begins."
    ),
    "implementer": (
        "Implement the card according to the acceptance criteria and architecture plan. "
        "Write clean, well-tested code that follows the project's conventions. Open a "
        "pull request with a clear description referencing the issue when done."
    ),
    "reviewer": (
        "Review the implementation against the architecture plan and acceptance criteria. "
        "Check for correctness, test coverage, code quality, and security concerns. "
        "Approve when all criteria are met; request changes with specific, actionable "
        "feedback otherwise."
    ),
    "security": (
        "Analyse the changes for OWASP Top 10 vulnerabilities, secret leakage, and "
        "insecure patterns. Block on critical and high severity findings. Post advisory "
        "comments for medium and low severity findings."
    ),
    "qa": (
        "Validate that the implementation satisfies every acceptance criterion by running "
        "the test suite and writing new tests for uncovered paths. Report failures with "
        "clear reproduction steps."
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
