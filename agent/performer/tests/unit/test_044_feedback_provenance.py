"""Actual backend prompts must preserve human answers and relay provenance."""
from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from performer.models import Score

BACKENDS = ["claude_code", "codex", "opencode", "opencode_compat", "hermes", "junie", "pi", "driver", "prime_agent", "openclaw"]
CORRECTION = "I have not authorized the next step. Ask and wait for my answer."
CONTINUATION = "Continue from the prior checkpoint. Next focus: implement step 2."


def prompt(backend: str, score: Score) -> str:
    module = importlib.import_module(f"performer.backends.{backend}")
    return module._build_task_prompt(score, []) if backend == "hermes" else module._build_task_prompt(score)


def score(**kwargs) -> Score:
    return Score(title="Synthetic", repo_url="https://github.com/example/sample", branch="synthetic", **kwargs)


@pytest.mark.parametrize("backend", BACKENDS)
def test_automatic_continuation_is_not_human_authorization(backend: str) -> None:
    text = prompt(backend, score(relay_feedback=[{"author_login": "coordinare", "source": "performer", "body": CONTINUATION}]))
    assert CONTINUATION in text
    assert "## Human Feedback" not in text
    assert "author: coordinare" in text
    assert "source: performer" in text
    assert "does not constitute human approval" in text


@pytest.mark.parametrize("backend", BACKENDS)
def test_current_human_correction_and_answer_survive_alongside_continuation(backend: str) -> None:
    text = prompt(backend, score(
        relay_feedback=[{"author_login": "coordinare", "body": CONTINUATION}],
        clarifications=[{"source": "issue", "comment_id": "123", "author": "human", "body": CORRECTION},
                        {"questions": ["Return blank on empty input?"], "answer": "Yes, return an empty string."}],
    ))
    for expected in [CORRECTION, "source: issue", "comment_id: 123", "author: human", "Return blank on empty input?", "Yes, return an empty string.", CONTINUATION]:
        assert expected in text
    assert "## Human Feedback" not in text


@pytest.mark.parametrize("backend", BACKENDS)
def test_review_identity_and_inline_location_are_preserved(backend: str) -> None:
    text = prompt(backend, score(relay_feedback=[{
        "author_login": "reviewer", "source": "review", "body": "Please change this case.",
        "comments": [{"body": "Keep this regression.", "path": "sample.py", "line": 12}],
    }]))
    for expected in ["author: reviewer", "source: review", "Please change this case.", "sample.py:12", "Keep this regression."]:
        assert expected in text


@pytest.mark.parametrize("backend", BACKENDS)
def test_unknown_feedback_source_is_not_invented(backend: str) -> None:
    text = prompt(backend, score(relay_feedback=[{"body": "Continue after checking the tests."}]))
    assert "source unspecified" in text
    assert "## Human Feedback" not in text


@pytest.mark.asyncio
async def test_junie_resumed_feedback_does_not_claim_a_human_source() -> None:
    from performer.backends.junie import JunieBackend

    backend = JunieBackend()
    backend._stand = SimpleNamespace(path="/tmp/synthetic")
    backend._original_prompt = "original task"
    backend.stop = AsyncMock()
    backend._launch = AsyncMock()
    await backend.relay_feedback(CONTINUATION)
    text = backend._launch.await_args.args[0]
    assert "## Human Feedback" not in text
    assert "source unspecified" in text
    assert CONTINUATION in text and "original task" in text


@pytest.mark.parametrize("raiser", ["reviewing", "qa", "security", "closing_review"])
def test_production_feedback_bounce_retains_its_raising_stage(raiser: str) -> None:
    from coordinare.graph.nodes.monitor.verdict import _stamp_feedback_bounce

    feedback = _stamp_feedback_bounce({}, [{"body": "Address this validation finding."}], raiser, "a" * 40)
    text = prompt("claude_code", score(relay_feedback=feedback))
    assert f"stage: {raiser}" in text
    assert "Address this validation finding." in text


def test_inline_feedback_retains_its_raising_stage() -> None:
    text = prompt("claude_code", score(relay_feedback=[{
        "author_login": "reviewer", "comments": [{"body": "Retain this assertion.", "path": "sample.py", "line": 9, "raiser": "qa"}],
    }]))
    assert "sample.py:9" in text and "Retain this assertion." in text
    assert "stage: qa" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
async def test_native_security_failure_reaches_the_implementer_prompt(backend: str) -> None:
    import json
    from pathlib import Path
    from unittest.mock import MagicMock

    from coordinare.graph.nodes.monitor_performer import monitor_performer
    from coordinare.graph.state import initial_state
    from performer.backends.base import BackendStatus
    from performer.main import handle_status
    from performer.models import Performance, Stand
    from performer.protocol import PerformerMessage

    native = MagicMock()
    native.get_status.return_value = BackendStatus(state="done", output=json.dumps({"security": {
        "verdict": "security_failed", "blocking": [{"category": "injection", "problem": "Unsafe query construction.",
        "why_blocking": "Input controls the query.", "evidence": "Unescaped user input.", "path": "sample.py", "line": 12,
        "severity": "high", "routing": "implementer"}],
    }}))
    perf = Performance(session_id="synthetic", stand=Stand(path=Path("/tmp/synthetic"), branch="synthetic"),
                       score=score(), backend=native, role="security", state="working")
    response = await handle_status(PerformerMessage(action="status", session_id="synthetic"), perf)
    assert response.status == "security_failed"
    state = initial_state()
    state.update(performer_services={"security": SimpleNamespace(check_status=AsyncMock(return_value=response.model_dump()))},
                 performer_stage="security", lifecycle_sequence=["implementing", "security"],
                 current_card={"id": "synthetic", "status": "IN_PROGRESS"}, agent_dispatch={"session_id": "synthetic"})
    result = await monitor_performer(state)
    assert result["phase"] == "dispatching" and result["performer_stage"] == "implementing"
    text = prompt(backend, score(relay_feedback=result["relay_feedback"]))
    for expected in ["[fb-1]", "Unsafe query construction.", "Input controls the query.", "Unescaped user input.", "sample.py:12", "stage: security"]:
        assert expected in text


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("stage", ["implementing", "reviewing"])
async def test_production_continuation_has_known_provenance(backend: str, stage: str) -> None:
    from unittest.mock import patch

    from coordinare.graph.nodes.monitor_performer import monitor_performer
    from coordinare.graph.state import initial_state

    service = SimpleNamespace(check_status=AsyncMock(return_value={
        "status": "partial_progress", "next_focus": CONTINUATION, "head_before": "a" * 40, "head_after": "b" * 40,
    }))
    state = initial_state()
    state.update(performer_services={stage: service}, performer_stage=stage,
                 lifecycle_sequence=["implementing", "reviewing"],
                 current_card={"id": "synthetic", "status": "IN_PROGRESS"}, agent_dispatch={"session_id": "synthetic"})
    with patch("coordinare.services.dispatch_guard.drain_or_reap", new=AsyncMock()):
        result = await monitor_performer(state)
    assert result["phase"] == "dispatching" and result["performer_stage"] == stage
    text = prompt(backend, score(relay_feedback=result["relay_feedback"]))
    for expected in [CONTINUATION, "author: coordinare", "source: performer", f"stage: {stage}", "does not constitute human approval"]:
        assert expected in text
    assert "## Human Feedback" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("kind", ["qa_workflow", "qa_legacy", "reviewer", "closer"])
async def test_native_qa_and_review_contracts_reach_the_implementer_prompt(backend: str, kind: str) -> None:
    import json
    from contextlib import ExitStack
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    from coordinare.graph.nodes.monitor_performer import monitor_performer
    from coordinare.graph.state import initial_state
    from performer.backends.base import BackendStatus
    from performer.main import handle_status
    from performer.models import Performance, Stand
    from performer.protocol import PerformerMessage, PerformerResponse

    if kind.startswith("qa"):
        role = "qa"
        payload = {"passed": False, "criteria_checked": 1, "criteria_passed": 0}
        if kind == "qa_workflow":
            payload.update(executed_checks=[{"command": "synthetic-check", "exit_code": 1, "output": "synthetic failure"}],
                           qa_findings=[{"category": "unexpected_regression", "criterion": "SYNTHETIC criterion",
                                         "expected": "SYNTHETIC expected value", "observed": "SYNTHETIC actual value"}])
        else:
            payload["failures"] = [{"criterion": "SYNTHETIC criterion", "expected": "SYNTHETIC expected value",
                                    "actual": "SYNTHETIC actual value", "test": "tests/test_synthetic.py::test_case"}]
        expected = ["SYNTHETIC criterion", "SYNTHETIC expected value", "SYNTHETIC actual value"]
        if kind == "qa_legacy":
            expected.append("tests/test_synthetic.py::test_case")
    elif kind == "reviewer":
        role = "reviewing"
        payload = {"review": {"verdict": "changes_requested", "findings": [{"path": "synthetic_review.py", "line": 23,
                   "category": "correctness", "problem": "SYNTHETIC review problem", "why_blocking": "SYNTHETIC review rationale"}]}}
        expected = ["synthetic_review.py:23", "SYNTHETIC review problem", "SYNTHETIC review rationale"]
    else:
        role = "closing_review"
        payload = {"closing": {"verdict": "changes_requested", "threads_read": 1, "classifications": [],
                   "open_threads": [{"path": "synthetic_closer.py", "line": 24, "excerpt": "SYNTHETIC unresolved comment", "thread_id": "synthetic-thread"}]}}
        expected = ["synthetic_closer.py:24", "SYNTHETIC unresolved comment"]
    native = MagicMock()
    native.get_status.return_value = BackendStatus(state="done", output=json.dumps(payload))
    perf = Performance(session_id="synthetic", stand=Stand(path=Path("/tmp/synthetic-unused"), branch="synthetic"),
                       score=score(), backend=native, role=role, state="working")
    with ExitStack() as stack:
        for name in ("commit_file", "post_pr_comment", "post_issue_comment", "get_head_sha"):
            stack.enter_context(patch("performer.main." + name, new=AsyncMock()))
        stack.enter_context(patch("performer.main.resolve_visual_evidence_urls", new=AsyncMock(return_value=[])))
        stack.enter_context(patch("performer.main.boot_and_capture_app_screenshot", return_value=None))
        stack.enter_context(patch("performer.workspace.consume_services_start_failure", return_value=None))
        response = await handle_status(PerformerMessage(action="status", session_id="synthetic"), perf)
    response = PerformerResponse.model_validate_json(response.model_dump_json())
    assert response.status == ("qa_failed" if role == "qa" else "changes_requested")
    state = initial_state()
    state.update(performer_services={role: SimpleNamespace(check_status=AsyncMock(return_value=response.model_dump()))},
                 performer_stage=role, lifecycle_sequence=["implementing", role],
                 current_card={"id": "synthetic", "status": "IN_PROGRESS"}, agent_dispatch={"session_id": "synthetic"})
    result = await monitor_performer(state)
    assert result["phase"] == "dispatching" and result["performer_stage"] == "implementing"
    text = prompt(backend, score(relay_feedback=result["relay_feedback"]))
    for value in [*expected, "[fb-1]", "stage: " + role]:
        assert value in text


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("role", ["reviewing", "closing_review"])
async def test_native_review_body_and_inline_comment_both_reach_the_prompt(backend: str, role: str) -> None:
    import json
    from copy import deepcopy
    from pathlib import Path
    from unittest.mock import MagicMock, patch

    from coordinare.graph.nodes.monitor_performer import monitor_performer
    from coordinare.graph.state import initial_state
    from performer.backends.base import BackendStatus
    from performer.main import handle_status
    from performer.models import Performance, Stand
    from performer.protocol import PerformerMessage, PerformerResponse

    summary = "SYNTHETIC summary: handle whitespace in every public input path."
    inline = "SYNTHETIC inline: retain this focused regression."
    native = MagicMock()
    native.get_status.return_value = BackendStatus(state="done", output=json.dumps({
        "approved": False, "body": summary,
        "comments": [{"path": "synthetic_review.py", "line": 23, "body": inline}],
    }))
    perf = Performance(session_id="synthetic", stand=Stand(path=Path("/tmp/synthetic-unused"), branch="synthetic"),
                       score=score(), backend=native, role=role, state="working",
                       pr_url="https://github.com/example/sample/pull/1")
    with patch("performer.main.post_pull_request_review", new=AsyncMock()):
        response = await handle_status(PerformerMessage(action="status", session_id="synthetic"), perf)
    response = PerformerResponse.model_validate_json(response.model_dump_json())
    assert response.status == "changes_requested" and response.body == summary
    wire = response.model_dump()
    unchanged = deepcopy(wire)
    state = initial_state()
    state.update(performer_services={role: SimpleNamespace(check_status=AsyncMock(return_value=wire))},
                 performer_stage=role, lifecycle_sequence=["implementing", role],
                 current_card={"id": "synthetic", "status": "IN_PROGRESS"}, agent_dispatch={"session_id": "synthetic"})
    result = await monitor_performer(state)
    assert result["phase"] == "dispatching" and result["performer_stage"] == "implementing"
    text = prompt(backend, score(relay_feedback=result["relay_feedback"]))
    for value in [summary, inline, "synthetic_review.py:23", "[fb-1]", "[fb-2]", "stage: " + role]:
        assert value in text
    assert wire == unchanged
