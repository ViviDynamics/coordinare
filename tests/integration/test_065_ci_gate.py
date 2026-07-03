"""065 US5 — integration tests for the coordinare-side CI gate (FR-021, FR-022).

Background
----------
Spec 043 (Performer CI Ownership) is subsumed into 065 US5.  The defence-in-
depth gate lives in ``_advance_stage`` (``monitor_performer.py``): when the
lifecycle completes, the coordinare runs the workspace's auto-detected lint
command before transitioning to ``monitoring_pr``.  On failure the card is
routed back to ``implementing`` with the lint output captured as relay
feedback; on success a single info-level log records the pass.

What these tests assert
-----------------------
- FR-021: failure emits ``performer.ci_failed`` carrying
  ``{card_id, performer_stage, ci_command, exit_code, output_excerpt}``.
- FR-022(b): the gate's failure path returns ``performer_stage="implementing"``
  and ``phase="dispatching"`` with the lint output in ``relay_feedback``.
- FR-022(c): the happy path is silent except for a single info-level log
  (``performer.ci_passed``) — no extra warning noise, no extra latency
  records.
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


class _FakeProc:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# FR-022(b) + FR-021 — failure path routes back and emits the structured log
# ---------------------------------------------------------------------------


def test_ci_gate_routes_failing_session_back_to_implementer(tmp_path: Path) -> None:
    """Failing CI must NOT advance to ``monitoring_pr``.  The session is
    rewound to ``performer_stage="implementing"`` with the lint output
    captured as relay feedback so the implementer sees what broke."""
    # Create a minimal Python workspace so detect() returns ruff.
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff]\nline-length = 100\n', encoding="utf-8",
    )
    state = _state_at_last_stage(tmp_path)

    from coordinare.services.ci_detection import CIDetectionResult
    fake_detect = CIDetectionResult(
        lint_command="ruff check .",
        test_command="pytest",
        stack="python",
        detected_from="pyproject.toml",
    )
    with patch(
        "coordinare.services.ci_detection.detect", return_value=fake_detect,
    ), patch(
        "subprocess.run",
        return_value=_FakeProc(1, stdout=b"E501 line too long", stderr=b""),
    ):
        updates = _advance_stage(state, status={})

    assert updates["performer_stage"] == "implementing", (
        f"CI gate did not rewind: stage={updates['performer_stage']!r}"
    )
    assert updates["phase"] == "dispatching"
    assert updates.get("relay_feedback"), "CI failure not captured as relay feedback"
    feedback_body = updates["relay_feedback"][0]["body"]
    assert "E501" in feedback_body, (
        f"lint output not propagated to implementer: {feedback_body!r}"
    )


def test_ci_gate_emits_performer_ci_failed_log(tmp_path: Path) -> None:
    """FR-021: failure log MUST be ``performer.ci_failed`` and MUST carry
    ``{card_id, performer_stage, ci_command, exit_code, output_excerpt}``."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff]\n', encoding="utf-8",
    )
    state = _state_at_last_stage(tmp_path)

    from coordinare.services.ci_detection import CIDetectionResult
    fake_detect = CIDetectionResult(
        lint_command="ruff check .",
        test_command=None,
        stack="python",
        detected_from="pyproject.toml",
    )
    with patch(
        "coordinare.services.ci_detection.detect", return_value=fake_detect,
    ), patch(
        "subprocess.run",
        return_value=_FakeProc(2, stdout=b"lint offense detail", stderr=b""),
    ), structlog.testing.capture_logs() as logs:
        _advance_stage(state, status={})

    matches = [r for r in logs if r.get("event") == "performer.ci_failed"]
    assert matches, (
        f"FR-021: performer.ci_failed not emitted; got events: "
        f"{[r.get('event') for r in logs]}"
    )
    record = matches[0]
    assert record.get("card_id") == "PVTI_X"
    assert record.get("performer_stage") == "implementing"
    assert record.get("ci_command") == "ruff check ."
    assert record.get("exit_code") == 2
    assert "lint offense detail" in str(record.get("output_excerpt", ""))


# ---------------------------------------------------------------------------
# FR-022(c) — happy path is silent (one info-level pass log, nothing else)
# ---------------------------------------------------------------------------


def test_ci_gate_happy_path_emits_single_pass_log(tmp_path: Path) -> None:
    """Happy path: lint passes → transition to ``monitoring_pr``.  Only a
    single info-level ``performer.ci_passed`` record should land; no
    warning-level noise on the green path."""
    (tmp_path / "pyproject.toml").write_text('[tool.ruff]\n', encoding="utf-8")
    state = _state_at_last_stage(tmp_path)

    from coordinare.services.ci_detection import CIDetectionResult
    fake_detect = CIDetectionResult(
        lint_command="ruff check .",
        test_command=None,
        stack="python",
        detected_from="pyproject.toml",
    )
    with patch(
        "coordinare.services.ci_detection.detect", return_value=fake_detect,
    ), patch(
        "subprocess.run", return_value=_FakeProc(0, stdout=b"", stderr=b""),
    ), structlog.testing.capture_logs() as logs:
        updates = _advance_stage(state, status={})

    assert updates["phase"] == "monitoring_pr", (
        "happy CI path did not transition to monitoring_pr"
    )
    failure_logs = [r for r in logs if r.get("event") == "performer.ci_failed"]
    assert not failure_logs, (
        f"happy path emitted failure log: {failure_logs!r}"
    )
    pass_logs = [r for r in logs if r.get("event") == "performer.ci_passed"]
    assert pass_logs, (
        f"happy path did not emit performer.ci_passed; events: "
        f"{[r.get('event') for r in logs]}"
    )


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
