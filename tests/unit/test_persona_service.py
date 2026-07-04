"""Unit tests for persona_service (018-performer-personas, T004)."""
from __future__ import annotations

import pytest
import yaml

from coordinare.config import PersonaConfig, PersonasConfig
from coordinare.services.persona_service import (
    DEFAULT_INSTRUCTIONS,
    PERSONA_MAX_LENGTH,
    VALID_ROLES,
    get_effective_instructions,
    reset_persona,
    save_persona,
)

# ---------------------------------------------------------------------------
# get_effective_instructions
# ---------------------------------------------------------------------------


def _personas(**kwargs: str) -> PersonasConfig:
    """Build a PersonasConfig with the given role overrides."""
    fields = {role: PersonaConfig(instructions=kwargs.get(role, "")) for role in VALID_ROLES}
    return PersonasConfig(**fields)


def test_returns_custom_when_set() -> None:
    personas = _personas(implementer="Always write tests first.")
    result = get_effective_instructions("implementer", personas)
    assert result == "Always write tests first."


def test_returns_default_when_empty_string() -> None:
    personas = _personas(implementer="")
    result = get_effective_instructions("implementer", personas)
    assert result == DEFAULT_INSTRUCTIONS["implementer"]
    assert result  # non-empty


def test_returns_default_when_whitespace_only() -> None:
    personas = _personas(implementer="   \n  ")
    result = get_effective_instructions("implementer", personas)
    assert result == DEFAULT_INSTRUCTIONS["implementer"]


def test_raises_for_unknown_role() -> None:
    personas = PersonasConfig()
    with pytest.raises(ValueError, match="Unknown role"):
        get_effective_instructions("wizard", personas)


def test_all_eight_roles_return_non_empty_default() -> None:
    """FR-001: all 8 roles must have non-empty default instructions."""
    personas = PersonasConfig()
    for role in (
        "advocate", "assessor", "architect", "implementer",
        "reviewer", "security", "qa", "tech_writer",
    ):
        result = get_effective_instructions(role, personas)
        assert result, f"Role {role!r} returned empty default instructions"


def test_qa_default_mentions_verification_and_visual_evidence() -> None:
    personas = PersonasConfig()
    qa_text = get_effective_instructions("qa", personas)
    assert "verification_steps" in qa_text
    assert "visual_evidence" in qa_text
    assert "visual_validation_required" in qa_text
    assert "demo_setup_steps" in qa_text
    assert "visual_capture_commands" in qa_text
    assert "visual_capture_blockers" in qa_text
    assert "reproduction steps" in qa_text.lower()


def test_implementer_default_forbids_weakening_tests() -> None:
    """090 (T030): the implementer persona carries the durable do-not-weaken
    mandate so the prohibition survives long runs even before the coordinare's
    independent test-integrity guard ever runs (FR-018, FR-020)."""
    personas = PersonasConfig()
    text = get_effective_instructions("implementer", personas)
    lower = text.lower()

    # The fix is the underlying code, never the test/check.
    assert "weaken" in lower
    # Every weakening vector the static guard rejects is named so the model
    # recognizes them as forbidden, not merely discouraged.
    for forbidden in ("skip", "xfail", "delete", "mock", "loosen"):
        assert forbidden in lower, forbidden
    # Hard stop + escalate rather than declaring DONE on a weakened check.
    assert "human" in lower


def test_tech_writer_default_maintains_wiki_grounded() -> None:
    """124 (US1/US3): the documenter persona maintains a source-grounded living
    docs/wiki with a README entrypoint, and never invents facts."""
    personas = PersonasConfig()
    text = get_effective_instructions("tech_writer", personas)
    lower = text.lower()

    assert "docs/wiki/" in text
    assert "docs/wiki/README.md" in text  # the entrypoint
    # Grounding is non-negotiable: no invented facts, read the code.
    assert "invent" in lower
    assert "ground" in lower
    # Still emits the standard {files} JSON commit contract.
    assert '"files"' in text


def test_tech_writer_default_preserves_pointer_files() -> None:
    """124 (review): the AGENTS.md/CLAUDE.md pointer task must instruct the model
    to PRESERVE existing file content (read-first, emit the COMPLETE file), so a
    weaker self-hosted model cannot clobber those shared files by emitting only
    the wiki section."""
    personas = PersonasConfig()
    text = get_effective_instructions("tech_writer", personas)
    lower = text.lower()

    assert "AGENTS.md" in text
    assert "CLAUDE.md" in text
    # Preserve existing content; never emit only the section.
    assert "preserve" in lower
    assert "never emit" in lower or "leave it untouched" in lower
    # The safety escape hatch: if you can't read it, don't overwrite it.
    assert "untouched" in lower


def test_tech_writer_default_has_significance_gate() -> None:
    """124 (feedback): the documenter runs on every card, so the persona must tell
    it to be conservative — return no files for cosmetic/trivial changes, and never
    keep a per-card changelog in the wiki. Prevents churn/noise on minor changes."""
    personas = PersonasConfig()
    text = get_effective_instructions("tech_writer", personas)
    lower = text.lower()

    assert "conservative" in lower
    # No-op (empty file list) for trivial changes rather than churning the wiki.
    assert '{"files": []}' in text
    # Names the trivial-change classes so the model recognizes them as skip-worthy.
    for trivial in ("typo", "cosmetic", "dependency", "test-only"):
        assert trivial in lower, trivial
    # The wiki is not a per-card changelog.
    assert "per-card changelog" in lower


def test_tech_writer_default_prunes_low_value_content() -> None:
    """124 (feedback): the documenter is a curator, not just an author — it audits
    the WHOLE existing wiki and trims low/no-value content (stale, thin stubs,
    duplication, changelog cruft), pruning noise but never signal."""
    personas = PersonasConfig()
    text = get_effective_instructions("tech_writer", personas)
    lower = text.lower()

    assert "prune" in lower
    assert "stale" in lower
    # Audits the whole wiki each run, not just the changed area.
    assert "whole wiki" in lower or "re-read the existing pages" in lower
    # Prune noise, not signal.
    assert "signal" in lower


def test_implementer_default_steers_off_workflow_files() -> None:
    """The implementer persona must proactively steer the agent away from
    editing `.github/workflows/**`, since the GitHub App token lacks
    `workflows` permission and any such push bounces the card. This is the
    preventive complement to the reactive workflow-push-permission relay."""
    personas = PersonasConfig()
    text = get_effective_instructions("implementer", personas)
    lower = text.lower()

    assert ".github/workflows/" in text
    assert "workflows" in lower
    # The instruction names the consequence so the model treats it as a hard
    # prohibition, and offers the escape hatch (describe the change for a human).
    assert "permission" in lower
    assert "human" in lower


def test_security_default_uses_taint_to_sink_checklist() -> None:
    """US2 (083): the security persona must drive a CWE taint→sink review.

    The model should trace untrusted sources to dangerous sinks across a
    fixed CWE checklist, and still emit the {passed, findings[]} contract
    with per-finding routing.
    """
    personas = PersonasConfig()
    text = get_effective_instructions("security", personas)
    lower = text.lower()

    # Taint-analysis framing: untrusted source -> dangerous sink.
    assert "untrusted" in lower
    assert "source" in lower
    assert "sink" in lower

    # Fixed CWE checklist (the six categories US2 mandates).
    assert "injection" in lower
    assert "authoriz" in lower  # broken authorization / authz
    assert "secret" in lower  # hardcoded secrets
    assert "deserializ" in lower  # insecure deserialization
    assert "path traversal" in lower
    assert "ssrf" in lower

    # Contract preserved: {passed, findings[]} JSON output + routing.
    assert "passed" in text
    assert "findings" in text
    assert "routing" in text


# ---------------------------------------------------------------------------
# save_persona / reset_persona
# ---------------------------------------------------------------------------


def _base_yaml() -> dict:
    """Return a minimal valid config dict suitable for writing as YAML."""
    return {
        "project_name": "Demo",
        "github_org": "acme",
        "github_project_number": 1,
        "github_token": "tok",
        "human_reviewers": ["alice"],
    }


def test_save_persona_raises_for_unknown_role(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    with pytest.raises(ValueError, match="Unknown role"):
        save_persona("wizard", "some text", config_path)


def test_save_persona_raises_when_instructions_exceed_max_length(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    long_instructions = "x" * (PERSONA_MAX_LENGTH + 1)
    with pytest.raises(ValueError, match="exceed maximum length"):
        save_persona("implementer", long_instructions, config_path)


def test_save_and_read_round_trip(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    raw = _base_yaml()
    config_path.write_text(yaml.dump(raw))

    save_persona("implementer", "Always use TDD.", config_path)

    loaded = yaml.safe_load(config_path.read_text())
    assert loaded["personas"]["implementer"]["instructions"] == "Always use TDD."


def test_save_persona_creates_personas_section_if_missing(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    raw = _base_yaml()
    config_path.write_text(yaml.dump(raw))

    save_persona("assessor", "Focus on value.", config_path)

    loaded = yaml.safe_load(config_path.read_text())
    assert "personas" in loaded
    assert loaded["personas"]["assessor"]["instructions"] == "Focus on value."


def test_save_persona_preserves_other_keys(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    raw = _base_yaml()
    raw["personas"] = {"implementer": {"instructions": "old"}}
    config_path.write_text(yaml.dump(raw))

    save_persona("assessor", "New.", config_path)

    loaded = yaml.safe_load(config_path.read_text())
    assert loaded["personas"]["implementer"]["instructions"] == "old"
    assert loaded["personas"]["assessor"]["instructions"] == "New."


def test_reset_persona_clears_instructions(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    raw = _base_yaml()
    raw["personas"] = {"implementer": {"instructions": "custom"}}
    config_path.write_text(yaml.dump(raw))

    reset_persona("implementer", config_path)

    loaded = yaml.safe_load(config_path.read_text())
    assert loaded["personas"]["implementer"]["instructions"] == ""


def test_save_persona_raises_on_nonexistent_file(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    assert not config_path.exists()

    with pytest.raises(ValueError, match="Config file does not exist"):
        save_persona("implementer", "Hello.", config_path)


def test_save_persona_accepts_exactly_max_length(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.dump(_base_yaml()))
    instructions = "x" * PERSONA_MAX_LENGTH
    save_persona("implementer", instructions, config_path)  # should not raise

    loaded = yaml.safe_load(config_path.read_text())
    assert len(loaded["personas"]["implementer"]["instructions"]) == PERSONA_MAX_LENGTH


# ---------------------------------------------------------------------------
# T021 — Hot-reload: no caching, on-demand reads
# ---------------------------------------------------------------------------


def test_hot_reload_no_caching(tmp_path) -> None:
    """T021: changing persona in config takes effect on next get_effective_instructions call.

    Verifies that get_effective_instructions reads from PersonasConfig each time
    (no module-level or service-level caching). The test simulates the daemon's
    behavior: config is loaded from YAML on each invocation.
    """
    import yaml

    from coordinare.config import ProjectConfiguration

    config_path = tmp_path / "config.yaml"
    base = {
        "project_name": "Demo",
        "github_org": "acme",
        "github_project_number": 1,
        "github_token": "tok",
        "human_reviewers": ["alice"],
    }

    # First load: no custom implementer persona → default
    config_path.write_text(yaml.dump(base))
    config1 = ProjectConfiguration.from_yaml(config_path)
    result1 = get_effective_instructions("implementer", config1.personas)
    assert result1 == DEFAULT_INSTRUCTIONS["implementer"]

    # Update config file with custom instructions
    base["personas"] = {"implementer": {"instructions": "Custom hot-reload value."}}
    config_path.write_text(yaml.dump(base))

    # Second load (no daemon restart): should reflect updated instructions
    config2 = ProjectConfiguration.from_yaml(config_path)
    result2 = get_effective_instructions("implementer", config2.personas)
    assert result2 == "Custom hot-reload value."

    # Confirm the two results are different
    assert result1 != result2


# ---------------------------------------------------------------------------
# 123 US2 (T009): QA persona is acceptance-criteria + visual only
# ---------------------------------------------------------------------------


def test_qa_persona_omits_lint_coverage_and_style() -> None:
    """123 FR-004: the QA persona no longer instructs the model to run linters,
    check test-coverage percentages, or report code-style issues."""
    qa = DEFAULT_INSTRUCTIONS["qa"].lower()
    assert "lint" not in qa
    assert "coverage" not in qa
    assert "code style" not in qa


def test_qa_persona_retains_acceptance_and_visual_evidence() -> None:
    """123 FR-005: the QA persona still centers acceptance-criteria verification
    and visual-evidence capture."""
    qa = DEFAULT_INSTRUCTIONS["qa"].lower()
    assert "acceptance criteri" in qa  # matches "criterion"/"criteria"
    assert "screenshot" in qa
    assert "visual_evidence" in DEFAULT_INSTRUCTIONS["qa"]


# ---------------------------------------------------------------------------
# 123 US7 (T020): closer persona is thread-resolution only
# ---------------------------------------------------------------------------


def test_closer_persona_omits_code_review_language() -> None:
    """123 FR-016: the closer no longer re-reviews code — no lint / code-quality
    / diff-review instructions remain."""
    closer = DEFAULT_INSTRUCTIONS["closer"].lower()
    assert "lint" not in closer
    assert "code quality" not in closer
    assert "diff review" not in closer


def test_closer_persona_retains_thread_resolution() -> None:
    """123 FR-015: the closer persona is about verifying reviewer threads are
    resolved and CI is passing."""
    closer = DEFAULT_INSTRUCTIONS["closer"].lower()
    assert "thread" in closer
    assert "resolv" in closer  # matches "resolved"/"resolution"
    assert "ci is passing" in closer


# ---------------------------------------------------------------------------
# 123 last-mile: QA persona must enforce boot-the-app-THEN-capture-THEN-verify
# (live QA passed 5/5 criteria but recorded a screenshot path that never existed
# because it ran Playwright without booting the app first)
# ---------------------------------------------------------------------------


def test_qa_persona_enforces_app_boot_before_capture() -> None:
    """The QA persona must tell the performer to START the app (if not serving)
    and confirm it responds BEFORE attempting a screenshot."""
    qa = DEFAULT_INSTRUCTIONS["qa"].lower()
    assert "start the app" in qa
    assert "do not attempt a screenshot against an app that is not serving" in qa


def test_qa_persona_enforces_screenshot_file_verification() -> None:
    """The QA persona must require verifying the screenshot FILE exists before
    recording it, and forbid claiming a path the capture did not create."""
    qa = DEFAULT_INSTRUCTIONS["qa"]
    assert "ls -la /tmp/qa_screenshot.png" in qa
    low = qa.lower()
    assert "never record a path" in low  # don't fake a nonexistent artifact


def test_qa_persona_uses_system_python_for_playwright() -> None:
    """The env-cache prepends a playwright-less python3 onto PATH, so the QA
    capture must invoke the image's system interpreter (/usr/local/bin/python3)
    explicitly — bare `python3` resolves to the cache's python and fails with
    ModuleNotFoundError (the real reason QA screenshots never materialized)."""
    qa = DEFAULT_INSTRUCTIONS["qa"]
    assert "/usr/local/bin/python3 - <<'EOF'" in qa  # capture uses the system python
    assert "shadow" in qa.lower()  # explains WHY bare python3 is wrong


def test_qa_persona_says_db_is_prestarted_and_forbids_initdb() -> None:
    """Coordinare's services-start already runs postgres/redis with the test DB
    created; a QA run wasted a whole cycle doing `initdb` itself (failed as
    root / non-root) → qa_env_blocked 0/5. The persona must tell QA the DB is
    already up and forbid managing its own."""
    qa = DEFAULT_INSTRUCTIONS["qa"].lower()
    assert "already running" in qa
    assert "initdb" in qa  # explicitly names what NOT to do
