"""The seam between the workflow's report and main.py's QA post-processing.

The scenario eval calls QAWorkflow().run() directly and never goes through
main.py, so this handoff was completely untested: the post-processing is what
commits new_test_files, uploads evidence to the qa-assets branch, and posts the
QA comment. A workflow that reasons perfectly still publishes nothing if its
report does not satisfy what that code consumes.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from performer.workflows.models import ExecutedCheck
from performer.workflows.qa import report as report_step
from performer.workflows.qa.models import CriterionVerdict, VisualDelta

MAIN = Path(__file__).resolve().parents[4] / "agent/performer/src/performer/main.py"


def _consumed_keys() -> set[str]:
    """Every key the QA post-processing reads, taken from the source itself.

    Derived rather than hardcoded: a list copied by hand goes stale the moment
    someone adds a `qa_output.get(...)`, and then this test passes while the
    seam it guards has drifted.
    """
    source = MAIN.read_text()
    block = source[source.index('if perf.role == "qa":'):]
    block = block[: block.index('if perf.role == "documenting":')]
    # BOTH access forms. The adversarial review named the exact mutation the
    # .get-only regex missed: change `qa_output.get("new_test_files", [])` to
    # `qa_output["new_test_files"]` and the key silently drops out of this set,
    # so the seam stops being guarded at precisely the point it starts being
    # able to crash.
    dotted = re.findall(r'qa_output\.get\(\s*"([a-z_]+)"', block)
    bracketed = re.findall(r'qa_output\[\s*"([a-z_]+)"\s*\]', block)
    return set(dotted) | set(bracketed)


def _report(**over):
    base = dict(
        verdicts=[CriterionVerdict(criterion="c", passed=True)],
        executed=[ExecutedCheck(plan_check_id="c1", command="pytest -q", exit_code=0, passed=True)],
        delta=VisualDelta(),
        findings=[],
        passed=True,
        visual_required=False,
        visual_evidence=[],
    )
    base.update(over)
    return report_step.build_report(**base)


def test_the_report_is_json_serialisable_as_the_adapter_sends_it():
    """The adapter hands the report to main.py as a JSON string."""
    payload = json.dumps(_report())
    assert json.loads(payload)["passed"] is True


def test_every_consumed_key_is_either_emitted_or_safely_defaulted():
    """A key the post-processing reads must either be present, or be read with
    a default. This fails loudly if someone adds a required read."""
    source = MAIN.read_text()
    block = source[source.index('if perf.role == "qa":'):]
    block = block[: block.index('if perf.role == "documenting":')]

    emitted = set(_report(visual_required=True))
    for key in _consumed_keys():
        bracketed = re.search(rf'qa_output\[\s*"{key}"\s*\]', block)
        if bracketed:
            # Bracket access cannot have a default: absence is a KeyError.
            assert key in emitted, (
                f"post-processing reads qa_output[{key!r}] directly -- the "
                f"workflow report MUST emit it or the run crashes"
            )
            continue
        defaulted = re.search(rf'qa_output\.get\(\s*"{key}"\s*,', block)
        assert key in emitted or defaulted, (
            f"post-processing reads {key!r} with no default and the workflow "
            f"report does not emit it"
        )


# --- 088 FR-005: boot proof for visual runs ---

def test_a_visual_run_emits_app_boot_check_backed_by_an_executed_check():
    """Defect 16.

    _qa_app_boot_evidence requires an app_boot_check whose command appears in
    executed_checks with exit 0. Without it, a visual run's screenshots drop out
    of the evidence count and the run folds into the unsubstantiated gate -- a
    correct QA run rejected by coordinare's own floor.

    The workflow BOOTS the app, so it can produce real boot proof instead of
    asking the model to assert it.
    """
    import sys

    sys.path.insert(0, str(MAIN.parents[2]))
    from performer.main import _qa_app_boot_evidence

    boot_check = ExecutedCheck(
        plan_check_id="app-boot",
        command="curl -fsS http://127.0.0.1:8000/",
        exit_code=0,
        passed=True,
    )
    report = _report(
        visual_required=True,
        executed=[boot_check],
        boot_check=boot_check,
    )

    parsed, ok = _qa_app_boot_evidence(report, report["executed_checks"])
    assert parsed is not None, "app_boot_check must be present for a visual run"
    assert ok, "the boot command must reference a real executed check with exit 0"


def test_boot_proof_is_absent_when_no_boot_happened():
    """Never fabricate boot proof: a run with no boot must not claim one."""
    import sys

    sys.path.insert(0, str(MAIN.parents[2]))
    from performer.main import _qa_app_boot_evidence

    report = _report(visual_required=True)
    _parsed, ok = _qa_app_boot_evidence(report, report["executed_checks"])
    assert not ok


def test_the_boot_check_appears_in_executed_checks_not_only_in_the_claim():
    """The floor requires the command to be IN executed_checks. A claim that
    references nothing is exactly the self-report spec 083 refuses."""
    boot_check = ExecutedCheck(
        plan_check_id="app-boot", command="curl -fsS http://127.0.0.1:8000/",
        exit_code=0, passed=True,
    )
    report = _report(visual_required=True, executed=[boot_check], boot_check=boot_check)
    commands = [c["command"] for c in report["executed_checks"]]
    assert report["app_boot_check"]["command"] in commands


# --- the SECOND consumer: coordinare's qa_verdict floor ---

QA_VERDICT = Path(__file__).resolve().parents[4] / "src/coordinare/services/qa_verdict.py"


def test_the_report_satisfies_coordinares_evidence_floor_too():
    """main.py is not the only consumer.

    coordinare's qa_verdict.py reads its own set of keys off the same report,
    and nothing tied the workflow's output to that set. Derived from source for
    the same reason as the main.py list: a hand-copied one goes stale silently.

    (An adversarial reviewer claimed qa_verdict validates a qa_findings SCHEMA
    and would crash on malformed findings. It does not reference findings at
    all -- the claim was speculation about a file it never opened. This test
    pins what it actually reads.)
    """
    source = QA_VERDICT.read_text()
    consumed = set(re.findall(r'report\.get\(\s*"([a-z_]+)"', source))
    assert consumed, "expected to find the keys qa_verdict reads"

    # Keys whose ABSENCE is itself the correct signal, listed deliberately so
    # the decision is visible rather than hidden in a regex. Adding a key here
    # is a claim that "missing" and "false/none" mean the same thing to every
    # consumer -- true for an error string, NOT true for a flag that
    # distinguishes "could not" from "did not need to".
    absence_is_meaningful = {
        "environment_error",  # absent == no environmental problem
    }

    emitted = set(_report(visual_required=True))
    for key in consumed - absence_is_meaningful:
        defaulted = re.search(rf'report\.get\(\s*"{key}"\s*,', source)
        assert key in emitted or defaulted, (
            f"qa_verdict reads {key!r} with no default and the workflow report "
            f"does not emit it"
        )


def test_qa_verdict_tolerates_a_report_missing_every_optional_key():
    """The behavioural counterpart to the syntactic check above: whatever the
    key list says, the floor must not crash on a minimal report."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))
    from coordinare.services.qa_verdict import qa_unsubstantiated_reason

    assert qa_unsubstantiated_reason({}) is None
    assert qa_unsubstantiated_reason({"criteria_checked": 2, "criteria_passed": 0})


def test_a_genuine_pass_survives_the_evidence_floor():
    """End of the road: a real workflow report must not be refused."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))
    from coordinare.services.qa_verdict import qa_unsubstantiated_reason

    boot_check = ExecutedCheck(
        plan_check_id="app-boot", command="curl -fsS http://127.0.0.1:8000/",
        exit_code=0, passed=True,
    )
    report = _report(
        visual_required=True,
        executed=[boot_check],
        boot_check=boot_check,
        visual_evidence=[{"label": "after", "path_or_url": "/tmp/qa_screenshot.png"}],
    )
    assert qa_unsubstantiated_reason(report) is None


def test_a_visual_report_with_no_evidence_is_still_refused():
    """The floor must keep biting: this is spec 120's whole purpose."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))
    from coordinare.services.qa_verdict import qa_unsubstantiated_reason

    report = _report(visual_required=True, visual_evidence=[])
    assert qa_unsubstantiated_reason(report) == "missing_visual_evidence"


def test_a_visual_run_that_captured_nothing_says_so():
    """"Could not capture" and "did not need to" must be distinguishable, or
    coordinare's capture-unavailable handling never fires."""
    assert _report(visual_required=True, visual_evidence=[])["visual_capture_unavailable"]


def test_a_visual_run_with_evidence_does_not_claim_capture_failed():
    report = _report(
        visual_required=True,
        visual_evidence=[{"label": "after", "path_or_url": "/tmp/shot.png"}],
    )
    assert "visual_capture_unavailable" not in report


def test_a_non_visual_run_never_claims_capture_failed():
    assert "visual_capture_unavailable" not in _report(visual_required=False)
