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

    # 082 finding — BACKEND_FORMAT_ERROR disambiguation. The performer emits this
    # code ONLY after the transport delivered output that the model would not
    # shape into the JSON contract (`Last output: <repr>`). A NON-EMPTY last
    # output means the model spoke but ignored the contract → model-quality miss
    # (FAIL_MODEL), NOT plumbing. Only an EMPTY last output (swallowed / transport
    # wall) stays FAIL_HARNESS. Pre-fix this was a blanket harness marker, so a
    # model that rubber-stamped prose got mislabelled as a fixable harness bug.
    def test_backend_format_error_with_output_is_model(self):
        blob = (
            '{"status":"error","reason":"BACKEND_FORMAT_ERROR: Backend security '
            "output could not be parsed as a JSON object after 2 attempts. "
            "Last output: \"It looks like you haven't specified what you'd like "
            'help with yet.\""} error error'
        )
        cat, _ = categorize("failed", blob, False, [], None)
        assert cat == "FAIL_MODEL"

    def test_backend_format_error_empty_output_is_harness(self):
        blob = (
            '{"status":"error","reason":"BACKEND_FORMAT_ERROR: Backend architecting '
            "output could not be parsed as a JSON object after 2 attempts. "
            "Last output: ''\"} error error"
        )
        cat, hit = categorize("failed", blob, False, [], None)
        assert cat == "FAIL_HARNESS"
        assert any("BACKEND_FORMAT_ERROR" in h for h in hit)


# --------------------------------------------------------------------------
# 082 finding (082r7 implementer/codex) — FR-009 mandates that a failed/error
# job state reconciled to PASS be EXPLICIT and DOCUMENTED. categorize() can
# legitimately route state=failed / error_code=error → PASS when the contract
# artifact holds (e.g. a real in-workspace pytest "4 passed"). When that
# override happens it must surface a reconciliation note so the discrepancy is
# never silent. reconciliation_note() is that pure signal.
# --------------------------------------------------------------------------
reconciliation_note = persona_bench.reconciliation_note


class TestReconciliationNote:
    def test_failed_state_passed_is_flagged(self):
        note = reconciliation_note("failed", "error", "PASS")
        assert note is not None
        assert "failed" in note and "error" in note

    def test_error_code_with_clean_state_is_flagged(self):
        # state succeeded but the result still carried error_code=error.
        note = reconciliation_note("succeeded", "error", "PASS")
        assert note is not None
        assert "error" in note

    def test_cancelled_state_passed_is_flagged(self):
        assert reconciliation_note("cancelled", None, "PASS") is not None

    def test_clean_pass_is_not_flagged(self):
        assert reconciliation_note("succeeded", None, "PASS") is None

    def test_non_pass_never_flagged(self):
        # A failed cell that stays FAIL_* / ERROR is already self-evident.
        assert reconciliation_note("failed", "error", "FAIL_MODEL") is None
        assert reconciliation_note("failed", "error", "ERROR") is None
        assert reconciliation_note("failed", "error", "FAIL_HARNESS") is None


# --------------------------------------------------------------------------
# 082 finding #1/#2 — the dispatch payload must be 080-aware: resolve the model
# from the role's mode (modes → model_endpoints → endpoints) and carry the
# planner/executor `orchestration` block for non-single strategies, exactly as
# the live coordinare does. Pre-080 the harness read performers.<role>.model and
# never injected orchestration, so dual-model never actually ran under bench.
# --------------------------------------------------------------------------
_DUAL_CONFIG = """
github_org: example-org
human_reviewers: [alice]
github_auth: pat
github_token: ghp_test_token
endpoints:
  - {name: ep-ollama, kind: ollama, base_url: "http://192.168.3.30:11434"}
  - {name: ep-litellm, kind: litellm, base_url: "https://litellm.example", auth_env: LITELLM_MASTER_KEY}
model_endpoints:
  - {name: plan-gptoss, endpoint: ep-ollama, model: "gpt-oss:120b"}
  - {name: exec-qwen, endpoint: ep-litellm, model: "spark/qwen3.6:35b"}
  - {name: solo-qwen, endpoint: ep-litellm, model: "spark/qwen3.6:35b"}
modes:
  - {name: plan-exec, strategy: always, thinking: plan-gptoss, tool: exec-qwen, expose_plan_as: thinking}
  - {name: solo, strategy: single, tool: solo-qwen}
performers:
  default: {backend: claude_code, mode: solo}
  architect: {backend: codex, mode: plan-exec}
  closer: {backend: pi, mode: solo}
"""


def _write_config(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(_DUAL_CONFIG)
    return str(p)


class TestResolveRoleDispatch:
    def test_dual_model_role_yields_orchestration(self, tmp_path):
        cfg_path = _write_config(tmp_path)
        out = persona_bench.resolve_role_dispatch(cfg_path, "architect")
        # executor leg is the dispatch model (mirrors live dispatch)
        assert out["model"] == "spark/qwen3.6:35b"
        orch = out["orchestration"]
        assert orch is not None
        assert orch["strategy"] == "always"
        assert orch["tool"]["model"] == "spark/qwen3.6:35b"
        assert orch["thinking"]["model"] == "gpt-oss:120b"

    def test_single_model_role_has_no_orchestration(self, tmp_path):
        cfg_path = _write_config(tmp_path)
        out = persona_bench.resolve_role_dispatch(cfg_path, "closer")
        assert out["model"] == "spark/qwen3.6:35b"
        assert out["orchestration"] is None


class TestBuildJobPayload:
    def _task(self):
        return persona_bench.RoleTask(
            "architect", "architecting", "", None,
            "T", "D", ["ac"], "persona", persona_bench.grade_architect, "rubric")

    def test_injects_orchestration_into_metadata(self):
        orch = {"strategy": "always", "tool": {"model": "x"}, "thinking": {"model": "y"}}
        payload = persona_bench.build_job_payload(
            self._task(), backend="codex", repo_url="r", branch="b",
            job_id="j", gh_token="t", extra_secrets={}, model="x",
            orchestration=orch, pr_url=None)
        assert payload["metadata"]["model"] == "x"
        assert payload["metadata"]["orchestration"] == orch
        assert payload["role"] == "architecting"
        assert payload["secrets"]["GITHUB_TOKEN"] == "t"

    def test_single_model_omits_orchestration(self):
        payload = persona_bench.build_job_payload(
            self._task(), backend="codex", repo_url="r", branch="b",
            job_id="j", gh_token="t", extra_secrets={}, model="x",
            orchestration=None, pr_url=None)
        assert "orchestration" not in payload["metadata"]

    def test_pr_url_attached_when_present(self):
        payload = persona_bench.build_job_payload(
            self._task(), backend="codex", repo_url="r", branch="b",
            job_id="j", gh_token="t", extra_secrets={}, model="x",
            orchestration=None, pr_url="https://example/pr/1")
        assert payload["pr_url"] == "https://example/pr/1"
        assert payload["metadata"]["pr_url"] == "https://example/pr/1"


# --------------------------------------------------------------------------
# Read-only fixture isolation: a performer must never push to the shared
# fixture branch (it commits a QA/work report). The bench forks a per-run
# throwaway branch from the fixture HEAD, dispatches there, and deletes it.
# --------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class TestParseOwnerRepo:
    def test_https_url_with_git_suffix(self):
        assert persona_bench._parse_owner_repo(
            "https://github.com/ViviDynamics/conductor-bench.git"
        ) == ("ViviDynamics", "conductor-bench")

    def test_https_url_without_git_suffix(self):
        assert persona_bench._parse_owner_repo(
            "https://github.com/ViviDynamics/conductor-bench"
        ) == ("ViviDynamics", "conductor-bench")

    def test_ssh_url(self):
        assert persona_bench._parse_owner_repo(
            "git@github.com:ViviDynamics/conductor-bench.git"
        ) == ("ViviDynamics", "conductor-bench")

    def test_token_embedded_https(self):
        assert persona_bench._parse_owner_repo(
            "https://x-access-token:abc@github.com/Owner/repo.git"
        ) == ("Owner", "repo")

    def test_non_github_url_returns_none(self):
        assert persona_bench._parse_owner_repo("https://example.com/foo") is None


class TestForkThrowawayBranch:
    def test_creates_ref_at_fixture_head(self, monkeypatch):
        calls = []

        def fake_api(method, path, token, json_body=None):
            calls.append((method, path, json_body))
            if method == "GET":
                return _FakeResp(200, {"object": {"sha": "deadbeef"}})
            return _FakeResp(201, {})

        monkeypatch.setattr(persona_bench, "_gh_api", fake_api)
        ok = persona_bench._fork_throwaway_branch(
            "https://github.com/O/r.git", "feat/qa-behavior", "bench-ro-qa-1", "t")
        assert ok is True
        # Resolved the fixture HEAD, then created the throwaway ref at that sha.
        assert calls[0] == ("GET", "/repos/O/r/git/ref/heads/feat/qa-behavior", None)
        assert calls[1] == ("POST", "/repos/O/r/git/refs",
                            {"ref": "refs/heads/bench-ro-qa-1", "sha": "deadbeef"})

    def test_returns_false_when_fixture_missing(self, monkeypatch):
        monkeypatch.setattr(persona_bench, "_gh_api",
                            lambda *a, **k: _FakeResp(404, {}))
        assert persona_bench._fork_throwaway_branch(
            "https://github.com/O/r.git", "nope", "bench-ro-x", "t") is False

    def test_returns_false_on_unparseable_repo(self, monkeypatch):
        monkeypatch.setattr(persona_bench, "_gh_api",
                            lambda *a, **k: _FakeResp(200, {"object": {"sha": "x"}}))
        assert persona_bench._fork_throwaway_branch(
            "https://example.com/foo", "b", "bench-ro-x", "t") is False


class TestDeleteRemoteBranch:
    def test_issues_delete_on_ref(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            persona_bench, "_gh_api",
            lambda method, path, token, json_body=None: calls.append((method, path))
            or _FakeResp(204, {}))
        persona_bench._delete_remote_branch(
            "https://github.com/O/r.git", "bench-ro-qa-1", "t")
        assert calls == [("DELETE", "/repos/O/r/git/refs/heads/bench-ro-qa-1")]

    def test_noop_on_unparseable_repo(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            persona_bench, "_gh_api",
            lambda *a, **k: called.append(1) or _FakeResp(204, {}))
        persona_bench._delete_remote_branch("https://example.com/foo", "b", "t")
        assert called == []


# --------------------------------------------------------------------------
# Judge eligibility: the LLM judge needs gradeable agent output. A job that
# reached terminal 'succeeded' always qualifies. A gate-refused cell ends
# state='failed' yet still carries a full structured QA report (ctx.parsed) —
# that must be scored too, so a refusal is behaviourally confirmed rather than
# falling through to the deterministic-only category. A genuinely crashed or
# cancelled job (no parsed report) has nothing to grade and stays skipped.
# --------------------------------------------------------------------------
class TestJudgeEligible:
    def test_succeeded_job_is_eligible(self):
        assert persona_bench._judge_eligible(
            _ctx(status="qa_passed", state="succeeded")) is True

    def test_succeeded_job_with_no_parsed_still_eligible(self):
        assert persona_bench._judge_eligible(
            _ctx(status="done", parsed={}, state="succeeded")) is True

    def test_gate_refused_failed_job_with_report_is_eligible(self):
        # The qwen3-14b guardfix case: state='failed', but the performer emitted
        # a structured qa_failed report — the judge should score it.
        ctx = _ctx(status="qa_failed", state="failed",
                   parsed={"status": "qa_failed", "failures": []},
                   summary='{"status":"qa_failed","failures":[]}')
        assert persona_bench._judge_eligible(ctx) is True

    def test_crashed_job_without_report_is_skipped(self):
        # No structured report (parsed empty) and not succeeded → nothing to grade.
        ctx = _ctx(status="", state="failed", parsed={},
                   summary="Traceback (most recent call last): ...")
        assert persona_bench._judge_eligible(ctx) is False

    def test_cancelled_job_without_report_is_skipped(self):
        assert persona_bench._judge_eligible(
            _ctx(status="", state="cancelled", parsed={})) is False
