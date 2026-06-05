"""Unit tests for the persona-capability benchmark graders + categorisation.

These cover the pure logic (deterministic graders against synthetic terminal
output, and the harness-vs-model categoriser) — no Docker / network. The live
matrix run is exercised separately via scripts/persona_bench.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

persona_bench = pytest.importorskip("persona_bench")
GradeCtx = persona_bench.GradeCtx
categorize = persona_bench.categorize


def _ctx(status="", summary="", parsed=None, state="succeeded"):
    return GradeCtx(role="r", state=state, status=status, summary=summary,
                    parsed=parsed or {}, error_code=None, cid=None, repo_subdir="/ws")


# --------------------------------------------------------------------------
# Deterministic graders (workspace-free roles).
# --------------------------------------------------------------------------
class TestReviewerGrader:
    def test_catches_planted_offbyone(self):
        c = _ctx(status="changes_requested",
                 summary='{"status":"changes_requested","comments":["the last item is skipped by range(len(items) - 1)"]}')
        ok, markers, _ = persona_bench.grade_reviewer(c)
        assert ok is True
        assert "last item" in markers

    def test_approving_buggy_pr_flagged(self):
        c = _ctx(status="approved", summary='{"status":"approved"}')
        ok, markers, _ = persona_bench.grade_reviewer(c)
        assert ok is False
        assert "APPROVED_BUGGY_PR" in markers


class TestSecurityGrader:
    def test_flags_injection(self):
        c = _ctx(status="security_failed",
                 summary='{"status":"security_failed","findings":["SQL injection via f-string","eval() of request data"]}')
        ok, markers, _ = persona_bench.grade_security(c)
        assert ok is True
        assert markers  # at least one vuln marker

    def test_passing_vuln_pr_flagged(self):
        c = _ctx(status="security_passed", summary='{"status":"security_passed"}')
        ok, markers, _ = persona_bench.grade_security(c)
        assert ok is False
        assert "PASSED_VULN_PR" in markers


class TestQaGrader:
    def test_rubber_stamp_detected(self):
        c = _ctx(status="qa_passed",
                 summary='{"status":"qa_passed","report":{"verification_steps":[]}}',
                 parsed={"status": "qa_passed", "report": {"verification_steps": []}})
        _ok, markers, _ = persona_bench.grade_qa(c)
        assert "RUBBER_STAMP" in markers

    def test_found_behavior_bug(self):
        parsed = {"status": "qa_failed", "report": {"verification_steps": ["ran checkout_total({}) -> KeyError"]}}
        c = _ctx(status="qa_failed",
                 summary='{"status":"qa_failed","failures":["KeyError on missing discount"]}',
                 parsed=parsed)
        ok, markers, _ = persona_bench.grade_qa(c)
        assert ok is True
        assert "found_behavior_bug" in markers


class TestAssessorGrader:
    def test_asks_questions(self):
        parsed = {"status": "assessment_complete", "sufficient": False,
                  "questions": ["What are the earning rules?", "Do points expire?"]}
        c = _ctx(status="assessment_complete", parsed=parsed, summary="...")
        ok, markers, _ = persona_bench.grade_assessor(c)
        assert ok is True
        assert "flagged_insufficient" in markers
        assert any("questions" in m for m in markers)


# --------------------------------------------------------------------------
# Categorisation (harness vs model vs pass) — the whole point of the tool.
# --------------------------------------------------------------------------
class TestCategorize:
    def test_thinking_unsupported_is_harness(self):
        cat, hit = categorize("succeeded",
                              "API Error 400 does not support thinking", False, [], None)
        assert cat == "FAIL_HARNESS"
        assert "does not support thinking" in hit

    def test_dispatch_error_is_harness(self):
        cat, _ = categorize("failed", "dispatch_error WorkspaceSetupError", False, [], None)
        assert cat == "FAIL_HARNESS"

    def test_terminal_failure_no_signature_is_error(self):
        cat, _ = categorize("failed", "model returned gibberish", False, [], None)
        assert cat == "ERROR"

    def test_pass_requires_contract_and_correct(self):
        assert categorize("succeeded", "ok", True, ["m"], True)[0] == "PASS"
        assert categorize("succeeded", "ok", True, ["m"], False)[0] == "FAIL_MODEL"
        assert categorize("succeeded", "ok", False, [], True)[0] == "FAIL_MODEL"

    def test_judge_authoritative_over_markers(self):
        # contract ok + markers present but judge says wrong → FAIL_MODEL
        assert categorize("succeeded", "ok", True, ["m"], False)[0] == "FAIL_MODEL"

    def test_deterministic_fallback_when_no_judge(self):
        # no judge: PASS needs contract_ok AND markers
        assert categorize("succeeded", "ok", True, ["m"], None)[0] == "PASS"
        assert categorize("succeeded", "ok", True, [], None)[0] == "FAIL_MODEL"
