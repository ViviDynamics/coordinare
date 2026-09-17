"""Deterministic tests for the eval's scoring logic (T046).

The scoring is ordinary code and belongs in the normal suite. Only the RUN
against a live model is nondeterministic, and that never enters CI.
"""
from __future__ import annotations

from coordinare.eval.qa_scenarios import ScenarioOutcome, ScenarioScore, classify


def test_classify_maps_a_report_onto_a_verdict_class():
    assert classify({"passed": True}).verdict == "pass"
    assert classify({"passed": False}).verdict == "fail"


def test_environment_error_outranks_the_verdict():
    """QA saying it could not verify is a different answer from QA saying no.
    A report carrying both must classify as the honest one."""
    assert classify({"passed": False, "environment_error": "app would not boot"}).verdict == (
        "environment_error"
    )


def test_findings_and_failures_both_feed_the_named_artifacts():
    """Both sources contribute -- but only their POSITIVE fields. This test
    once asserted that `actual` prose ("Password missing") fed the match; that
    was the negation hole, so the assertion now pins the opposite."""
    outcome = classify({
        "passed": False,
        "failures": [{"criterion": "sign in", "expected": "Password field present",
                      "actual": "no password issue, honestly"}],
        "qa_findings": [{"expected": "Workspace present", "observed": "absent"}],
    })
    joined = " ".join(outcome.named)
    assert "Password field present" in joined, "failures contribute via expected/criterion"
    assert "Workspace present" in joined, "findings contribute via expected"
    assert "honestly" not in joined, "model prose (actual/observed) is excluded"


def test_a_right_verdict_for_the_wrong_reason_does_not_score():
    """The point of must_name. A scenario that fails for an unrelated reason
    tells us nothing about whether QA can see the defect."""
    score = ScenarioScore(
        name="regression", expected="fail",
        runs=[ScenarioOutcome("fail", ["some unrelated complaint"])] * 3,
    )
    assert score.correct == 3, "the verdict class is right"
    assert score.named_correctly(["Password"]) == 0, "but for the wrong reason"
    assert "wrong reason" in score.line(["Password"])


def test_a_right_verdict_for_the_right_reason_scores():
    score = ScenarioScore(
        name="regression", expected="fail",
        runs=[ScenarioOutcome("fail", ["the Password field is absent"])] * 3,
    )
    assert score.named_correctly(["Password"]) == 3
    assert "wrong reason" not in score.line(["Password"])


def test_a_scenario_with_no_required_names_scores_on_verdict_alone():
    score = ScenarioScore(name="healthy", expected="pass",
                          runs=[ScenarioOutcome("pass")] * 2)
    assert score.named_correctly([]) == 2


def test_a_wrong_verdict_never_scores_however_well_worded():
    score = ScenarioScore(
        name="healthy", expected="pass",
        runs=[ScenarioOutcome("fail", ["Password", "Workspace"])] * 3,
    )
    assert score.correct == 0
    assert score.named_correctly(["Password"]) == 0


def test_a_crashed_run_counts_as_incorrect_rather_than_being_dropped():
    """Silently dropping errored runs would inflate the rate."""
    score = ScenarioScore(
        name="regression", expected="fail",
        runs=[ScenarioOutcome("fail", ["Password"]), ScenarioOutcome("error", error="boom")],
    )
    assert score.correct == 1
    assert "1/2" in score.line(["Password"])


def test_no_collected_test_reaches_the_live_gateway():
    """The CI boundary, pinned rather than trusted.

    pytest's testpaths includes `tests`, so everything here IS collected. The
    deterministic parts (generator, scoring) belong in CI; the scoring RUN
    against a real model does not, and it stays out by living in a module that
    only `python -m` invokes.

    This asserts no collected test file imports the gateway binding, so wiring
    the live eval into CI becomes a deliberate act that breaks a test rather
    than a helpful-looking edit.
    """
    import ast
    from pathlib import Path

    eval_tests = Path(__file__).parent
    offenders = []
    for path in eval_tests.glob("test_*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            # Real imports only. Matching on raw text would flag this test's
            # own error message, which is how the first version of this failed.
            if (isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "coordinare.eval.gateway",
            )) or (isinstance(node, ast.Import) and any(
                a.name.startswith("coordinare.eval.gateway") for a in node.names
            )):
                offenders.append(path.name)

    assert not offenders, (
        f"{sorted(set(offenders))} import the live-gateway binding; see "
        "README.md — the scored eval must not run in CI (Constitution II)"
    )


def test_a_negation_in_prose_does_not_satisfy_must_name():
    """Round-two review, critical: must_name was a substring match over free
    prose, so "no password issue found" satisfied must_name=["Password"] and a
    false pass could score as a true positive."""
    outcome = classify({
        "passed": False,
        "failures": [{"criterion": "sign in works", "expected": "sign in works",
                      "actual": "no password issue found; the form has a password field"}],
    })
    score = ScenarioScore(name="regression", expected="fail", runs=[outcome])
    assert score.named_correctly(["Password"]) == 0


def test_must_name_matches_the_artifact_named_positively():
    """The artifact is named in `expected` / `criterion` -- fields our code
    builds from the lost element or the criterion text, never model prose."""
    outcome = classify({
        "passed": False,
        "qa_findings": [{"category": "unexpected_regression",
                         "expected": "text_input present (label='password')",
                         "observed": "text_input absent after the change"}],
    })
    score = ScenarioScore(name="regression", expected="fail", runs=[outcome])
    assert score.named_correctly(["Password"]) == 1
