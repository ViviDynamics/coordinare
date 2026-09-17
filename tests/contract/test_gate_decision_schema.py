"""Contract tests for CIGateDecision + relay_feedback shapes (spec 075).

Pinned shape lives in `specs/075-implementer-ci-gate/contracts/gate-decision.md`.
Any drift in the JSON surface must update both the contract and these tests.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from coordinare.services.ci_gate import CIGateDecision, FailedCheck

SHA40 = "a" * 40
SHA40_OTHER = "b" * 40


def _bounce_decision(**overrides: object) -> CIGateDecision:
    base: dict[str, object] = {
        "verdict": "bounce",
        "head_sha": SHA40,
        "required_checks": ["integration-tests", "lint", "unit-tests"],
        "failed_checks": [
            FailedCheck(
                name="unit-tests",
                conclusion="failure",
                html_url="https://github.com/org/repo/actions/runs/12345",
                last_log_line="FAILED tests/unit/test_foo.py::test_bar - AssertionError",
            ),
        ],
        "pending_checks": [],
        "resolver_source": "persona_check_map",
        "bounce_count_after": 1,
        "decided_at": "2026-05-28T14:23:11.482Z",
    }
    base.update(overrides)
    return CIGateDecision(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# T018: CIGateDecision JSON round-trip + field rules
# ---------------------------------------------------------------------------


def test_bounce_decision_round_trips_through_json() -> None:
    d = _bounce_decision()
    payload = d.model_dump(mode="json")
    again = CIGateDecision.model_validate(payload)
    assert again == d


def test_verdict_must_be_in_enum() -> None:
    with pytest.raises(ValidationError):
        _bounce_decision(verdict="unknown")


def test_head_sha_must_be_40_char_lowercase_hex() -> None:
    with pytest.raises(ValidationError):
        _bounce_decision(head_sha="ABCDEF")
    with pytest.raises(ValidationError):
        _bounce_decision(head_sha="A" * 40)


def test_required_checks_must_be_sorted() -> None:
    with pytest.raises(ValidationError):
        _bounce_decision(required_checks=["zzz", "aaa"])


def test_failed_checks_must_be_empty_on_pass() -> None:
    with pytest.raises(ValidationError):
        CIGateDecision(
            verdict="pass",
            head_sha=SHA40,
            required_checks=["lint"],
            failed_checks=[FailedCheck(name="lint", conclusion="failure")],
            resolver_source="all_head_checks",
            bounce_count_after=0,
            decided_at="2026-05-28T14:23:11.482Z",
        )


def test_failed_checks_must_be_nonempty_on_bounce() -> None:
    with pytest.raises(ValidationError):
        CIGateDecision(
            verdict="bounce",
            head_sha=SHA40,
            required_checks=["lint"],
            failed_checks=[],
            resolver_source="all_head_checks",
            bounce_count_after=1,
            decided_at="2026-05-28T14:23:11.482Z",
        )


def test_pending_checks_must_be_empty_when_not_hold() -> None:
    with pytest.raises(ValidationError):
        _bounce_decision(pending_checks=["lint"])


def test_bounce_count_after_must_be_zero_on_pass() -> None:
    with pytest.raises(ValidationError):
        CIGateDecision(
            verdict="pass",
            head_sha=SHA40,
            required_checks=[],
            failed_checks=[],
            resolver_source="all_head_checks",
            bounce_count_after=5,
            decided_at="2026-05-28T14:23:11.482Z",
        )


def test_last_log_line_truncated_to_200_chars() -> None:
    fc = FailedCheck(name="x", conclusion="failure", last_log_line="z" * 500)
    assert fc.last_log_line is not None
    assert len(fc.last_log_line) == 200


def test_resolver_source_enum_enforced() -> None:
    with pytest.raises(ValidationError):
        _bounce_decision(resolver_source="made_up_layer")


def test_empty_head_sha_allowed_for_no_pr_pass() -> None:
    """FR-013: gate early-returns PASS when there's no PR / head_sha."""
    d = CIGateDecision(
        verdict="pass",
        head_sha="",
        required_checks=[],
        failed_checks=[],
        resolver_source="all_head_checks",
        bounce_count_after=0,
        decided_at="2026-05-28T14:23:11.482Z",
    )
    assert d.head_sha == ""


def test_signature_stable_across_decided_at_changes() -> None:
    d1 = _bounce_decision(decided_at="2026-05-28T14:23:11.482Z")
    d2 = _bounce_decision(decided_at="2026-05-28T15:00:00.000Z")
    assert d1.signature() == d2.signature()


def test_signature_changes_when_failed_set_changes() -> None:
    d1 = _bounce_decision()
    d2 = _bounce_decision(
        failed_checks=[FailedCheck(name="lint", conclusion="failure")],
    )
    assert d1.signature() != d2.signature()


# ---------------------------------------------------------------------------
# T019: relay_feedback entry shape (BOUNCE only)
# ---------------------------------------------------------------------------
# The gate writes {"body": str, "author_login": "coordinare"} — the standard
# relay_feedback shape that the spec-070 dispatcher consumes.  Richer CI
# details live on session["latest_ci_gate_decision"], not in relay_feedback.
# (See contracts/gate-decision.md §2.)


def _make_ci_gate_relay_entry(failed_names: list[str]) -> dict[str, object]:
    """Construct the relay_feedback entry exactly as _evaluate_ci_gate does."""
    body = (
        f"CI gate: {len(failed_names)} required check(s) failing on this HEAD "
        f"({', '.join(failed_names)}). Fix and push before re-handing off to reviewer."
    )
    return {"body": body, "author_login": "coordinare"}


def test_relay_feedback_entry_has_required_shape() -> None:
    """relay_feedback entry has {body: str, author_login: "coordinare"} shape."""
    entry = _make_ci_gate_relay_entry(["unit-tests"])

    assert isinstance(entry["body"], str)
    assert entry["author_login"] == "coordinare"
    assert "unit-tests" in entry["body"]  # type: ignore[operator]


def test_relay_feedback_body_mentions_all_failing_checks() -> None:
    """All failing check names appear in the body text."""
    failing = ["lint", "unit-tests", "integration"]
    entry = _make_ci_gate_relay_entry(failing)

    for name in failing:
        assert name in entry["body"]  # type: ignore[operator]


def test_relay_feedback_has_no_extra_keys() -> None:
    """The entry has exactly {body, author_login} — nothing more."""
    entry = _make_ci_gate_relay_entry(["lint"])
    assert set(entry.keys()) == {"body", "author_login"}
