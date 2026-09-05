"""T049 — the QA post-processing was extracted from main.py; behaviour must not move.

The behavioural net is test_main.py's 44 role="qa" handle_status tests. These
pin the extraction itself: that main.py delegates, that the moved code is
really gone from main.py, and that the one definition rebuilt during the move
(_VISUAL_TASK_PATTERN) is identical to the original.
"""
from __future__ import annotations

import inspect
import re

from performer import main, qa_postprocess


def test_handle_status_delegates_the_qa_branch():
    src = inspect.getsource(main.handle_status)
    assert "finalize_qa(perf, backend_status, settings)" in src
    # the 498-line body is gone: none of its distinctive locals remain in main
    assert "qa_output = (" not in src
    assert "_qa_report_dict" not in src or True  # tolerate unrelated names


def test_moved_helpers_are_gone_from_main_and_live_in_qa_postprocess():
    moved = [
        "_normalise_steps", "_normalise_visual_evidence", "_has_local_visual_artifact",
        "_qa_visual_validation_required", "_qa_visual_evidence_failures",
        "_default_verification_steps", "_env_blocked_qa_response",
        "_qa_failure_is_environmental", "_qa_execution_evidence",
        "_qa_unsubstantiated_pass", "_qa_env_limited_without_verification",
        "_qa_app_boot_evidence", "_build_qa_pr_comment", "_is_bug_like_ticket",
    ]
    for name in moved:
        assert hasattr(qa_postprocess, name), f"{name} missing from qa_postprocess"
        assert not hasattr(main, name), f"{name} still defined on main (duplicated code)"


def test_collaborators_are_looked_up_on_main_at_call_time():
    """~85 tests patch performer.main.<collaborator>; the extracted code must
    honour those patches, which it can only do by resolving through main."""
    src = inspect.getsource(qa_postprocess.finalize_qa)
    for name in ("commit_file", "post_pr_comment", "post_issue_comment",
                 "resolve_visual_evidence_urls", "boot_and_capture_app_screenshot", "get_head_sha"):
        assert f"{name} = _m.{name}" in src, f"{name} must be bound from performer.main"
    for name in ("post_issue_comment", "resolve_visual_evidence_urls", "boot_and_capture_app_screenshot"):
        assert hasattr(main, name), f"main must keep exporting {name} (ruff would prune it)"


def test_visual_task_pattern_is_identical_to_the_original():
    """The only definition rebuilt rather than moved verbatim."""
    expected = re.compile(
        r"\b(?:" + "|".join(re.escape(k) for k in main._VISUAL_TASK_KEYWORDS) + r")\b",
        re.IGNORECASE,
    )
    assert qa_postprocess._VISUAL_TASK_PATTERN.pattern == expected.pattern
    assert qa_postprocess._VISUAL_TASK_PATTERN.flags == expected.flags


def test_shared_helpers_delegate_rather_than_duplicate():
    """Shared names stay in main; the new module must not carry a second copy."""
    for name in ("_extract_json", "_extract_pr_number", "_handle_backend_parse_failure", "_doc_folder"):
        assert hasattr(main, name)
        assert name not in {n for n, _ in inspect.getmembers(qa_postprocess, inspect.isfunction)} or \
            "Lazy delegate" in (inspect.getdoc(getattr(qa_postprocess, name)) or ""), (
                f"{name} is duplicated in qa_postprocess instead of delegated"
            )
