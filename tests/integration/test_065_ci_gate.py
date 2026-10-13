"""065 US5 / #106 — where the lint gate lives.

Background
----------
Spec 043 (Performer CI Ownership) was subsumed into 065 US5, which added a
"defence-in-depth" gate to ``_advance_stage``: at the end of the lifecycle the
daemon ran the workspace's auto-detected lint command itself.

#106 removed that. Coordinare is an orchestrator and never executes a
repo-derived command. The gate also checked the wrong tree for container
performers (the daemon's clone does not contain the performer's commits) and
did nothing on Kubernetes (no daemon clone). Lint is owned by the performer
(043 pre-commit check, 089 local test gate) and CI is read through the PR
checks gate.

What these tests assert
-----------------------
- #106: completing the lifecycle executes nothing in the daemon, even with a
  lintable workspace on disk, and transitions to ``monitoring_pr``.
- FR-022(d): rendered reviewer / closer personas contain the explicit lint
  ownership directive.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import structlog

from coordinare.graph.nodes.monitor_performer import _advance_stage


def _state_at_last_stage(workspace: Path) -> dict[str, Any]:
    """Build a state that is one ``_advance_stage`` call away from
    transitioning to ``monitoring_pr`` — i.e. the CI gate WILL run."""
    return {
        "lifecycle_sequence": ["implementing"],
        "performer_stage": "implementing",
        "current_card": {"id": "PVTI_X", "status": "IN_PROGRESS"},
        "workspace_path": workspace,
    }


# ---------------------------------------------------------------------------
# #106 — the gate is gone from the daemon: nothing repo-derived is executed
# ---------------------------------------------------------------------------


def _lintable_workspace(tmp_path: Path) -> Path:
    """A workspace ci_detection would have found a lint command for."""
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")
    (tmp_path / "package.json").write_text('{"scripts": {"lint": "eslint ."}}\n')
    return tmp_path


def test_last_stage_advances_without_executing_anything(tmp_path: Path) -> None:
    """Coordinare is an orchestrator: with a workspace on disk that carries a
    detectable lint command, completing the lifecycle spawns no process and
    goes straight to ``monitoring_pr``."""
    state = _state_at_last_stage(_lintable_workspace(tmp_path))

    with patch("subprocess.run", side_effect=AssertionError("daemon executed a command")), \
         patch("subprocess.Popen", side_effect=AssertionError("daemon spawned a process")), \
         structlog.testing.capture_logs() as logs:
        updates = _advance_stage(state)

    assert updates["phase"] == "monitoring_pr"
    assert updates["current_card"]["status"] == "IN_REVIEW"
    assert "relay_feedback" not in updates
    events = {r.get("event") for r in logs}
    assert not events & {"performer.ci_failed", "performer.ci_passed", "ci_gate.lint_error"}


def test_daemon_no_longer_exposes_the_lint_gate() -> None:
    from coordinare.graph.nodes.monitor import verdict

    assert not hasattr(verdict, "_ci_lint_gate")
    import inspect

    source = inspect.getsource(verdict)
    assert "subprocess" not in source and "shlex" not in source


# ---------------------------------------------------------------------------
# FR-022(d) — reviewer / closer personas carry the lint ownership directive
# ---------------------------------------------------------------------------


def test_reviewer_persona_contains_ci_ownership_directive() -> None:
    """The reviewer's rendered persona MUST include the CI-ownership
    directive so reviewers explicitly run lint against the diff."""
    from coordinare.services.persona_service import DEFAULT_INSTRUCTIONS

    reviewer = DEFAULT_INSTRUCTIONS.get("reviewer", "")
    lowered = reviewer.lower()
    assert (
        "ci ownership" in lowered
        or ("lint" in lowered and "do not approve" in lowered)
    ), (
        "reviewer persona missing CI/lint ownership directive — text was:\n"
        f"{reviewer[:600]}"
    )


def test_closer_persona_is_thread_resolution_verifier() -> None:
    """123 US7 (FR-017/FR-018): the closer is a lightweight thread-resolution
    verifier — it confirms open reviewer threads are resolved and CI is passing,
    and does NOT own linting / code re-review (that is the reviewer's job; the
    coordinare's spec-064 gate owns the authoritative CI decision)."""
    from coordinare.services.persona_service import DEFAULT_INSTRUCTIONS

    closer = DEFAULT_INSTRUCTIONS.get("closer", "")
    lowered = closer.lower()
    # FR-018: verifies thread resolution + CI passing.
    assert "thread" in lowered and "resolv" in lowered and "ci is passing" in lowered, (
        "closer persona missing thread-resolution / CI-passing language — text was:\n"
        f"{closer[:600]}"
    )
    # FR-017: no lint / code-quality / diff-review ownership.
    assert "lint" not in lowered
    assert "code quality" not in lowered
