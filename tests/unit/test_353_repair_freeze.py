"""An execution repair may change only its failure closure; every other established record is frozen."""

from __future__ import annotations

import pytest
from coordinare_service_inference.environment.models import (
    CheckResult,
    Command,
    EnvironmentManifest,
    VerificationReport,
)

from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_obligations import guard_repair_diff
from coordinare.services.environment_repair_triage import triage_failure
from tests.unit.env_manifest_fixture import reviewed_metadata


def cmd(identifier, argv=("true",), **extra):
    return {"id": identifier, "argv": list(argv), **extra}


def build(*, install=(), checks=(), activation=None, requirements=(), criterion_checks=None):
    data = reviewed_metadata(install=list(install), checks=list(checks), activation=activation or {})["environment_manifest"]
    data["requirements"] = list(requirements)
    manifest = EnvironmentManifest.model_validate(data)
    return manifest.model_copy(update={"criterion_checks": dict(criterion_checks or {})})


def failed(manifest, identifier, *, not_run=()):
    results = [CheckResult(id=identifier, status="fail", required=True, exit_code=1, duration_seconds=1)]
    results += [CheckResult(id=item, status="not_run", required=True) for item in not_run]
    return VerificationReport(manifest_digest=manifest.digest(), results=results)


def base():
    """A failed first generation: install-tool fails; the other install and both checks never ran (preflight)."""
    return build(
        install=[cmd("install-base"), cmd("install-tool", ["make", "tool"], depends_on=["install-base"]),
                 cmd("install-other", ["make", "other"])],
        checks=[cmd("check-tool", ["tool"]), cmd("check-other", ["other"])],
        criterion_checks={"S0123456789ABCDEF": ["check-other"]},
    )


def triage_of(previous):
    return triage_failure(previous, failed(previous, "install-tool", not_run=["install-base", "check-tool", "install-other",
                                                                             "check-other"]))


def with_install(manifest, identifier, **changes):
    install = [item.model_copy(update=changes) if item.id == identifier else item for item in manifest.install]
    return manifest.model_copy(update={"install": install})


def expect_rejected(candidate, previous, triage, *ids):
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        guard_repair_diff(candidate, previous, triage)
    assert exc.value.code == "repair_outside_closure"
    for identifier in ids:
        assert identifier in str(exc.value)


def test_the_closure_may_change_and_nothing_else_may():
    previous = base()
    triage = triage_of(previous)
    candidate = with_install(previous, "install-tool", argv=["make", "tool", "PREFIX=/opt"])

    guard_repair_diff(candidate, previous, triage)


def test_an_unexecuted_record_outside_the_closure_is_frozen_too():
    previous = base()
    triage = triage_of(previous)
    candidate = with_install(previous, "install-other", argv=["make", "other", "-j1"])

    expect_rejected(candidate, previous, triage, "install-other")


def test_a_frozen_check_cannot_be_weakened_or_removed_and_both_ids_are_named():
    previous = base()
    triage = triage_of(previous)
    weakened = previous.model_copy(update={"checks": [check.model_copy(update={"timeout_seconds": 1})
                                                      if check.id == "check-other" else check for check in previous.checks]})
    expect_rejected(weakened, previous, triage, "check-other")
    removed = previous.model_copy(update={"checks": [check for check in previous.checks if check.id != "check-other"]})
    expect_rejected(removed, previous, triage, "check-other")


def test_added_records_are_allowed_and_are_judged_by_review_not_by_the_guard():
    previous = base()
    triage = triage_of(previous)
    added = cmd("install-extra", ["make", "extra"])
    candidate = previous.model_copy(update={"install": [*previous.install, Command.model_validate(added)]})

    guard_repair_diff(candidate, previous, triage)


def test_a_frozen_mapping_may_gain_an_added_check_but_not_lose_one():
    previous = base()
    triage = triage_of(previous)
    grown = previous.model_copy(update={"criterion_checks": {"S0123456789ABCDEF": ["check-other", "check-new"]}})
    guard_repair_diff(grown.model_copy(update={"checks": [*grown.checks, Command.model_validate(cmd("check-new", ["new"]))]}),
                      previous, triage)

    lost = previous.model_copy(update={"criterion_checks": {"S0123456789ABCDEF": []}})
    expect_rejected(lost, previous, triage, "S0123456789ABCDEF")


def test_a_frozen_mapping_cannot_point_at_an_existing_check_it_did_not_have():
    previous = base()
    triage = triage_of(previous)
    moved = previous.model_copy(update={"criterion_checks": {"S0123456789ABCDEF": ["check-other", "check-tool"]}})

    expect_rejected(moved, previous, triage, "S0123456789ABCDEF")


def test_activation_is_frozen_unless_the_failure_implicates_it():
    previous = build(install=[cmd("install-tool", ["make"])], activation={"path": ["{cache}/bin"], "env": {"A": "1"}},
                     checks=[cmd("check-tool", ["tool"])])
    failure = VerificationReport(manifest_digest=previous.digest(), results=[
        CheckResult(id="check-tool", status="fail", required=True, exit_code=1, duration_seconds=1)])
    triage = triage_failure(previous, failure)
    grown = previous.model_copy(update={"activation": previous.activation.model_copy(update={"path": ["{cache}/bin", "{cache}/x"],
                                                                                          "env": {"A": "1", "B": "2"}})})
    guard_repair_diff(grown, previous, triage)

    rewritten = previous.model_copy(update={"activation": previous.activation.model_copy(update={"path": ["{cache}/other"]})})
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        guard_repair_diff(rewritten, previous, triage)
    assert exc.value.code == "repair_outside_closure" and "activation" in str(exc.value)

    # A failed activation is the only implicated record: the same rewrite is then inside the closure.
    failing = previous.model_copy(update={"activation": previous.activation.model_copy(update={"path": ["{cache}/bin"]})})
    activation_failure = VerificationReport(manifest_digest=failing.digest(), results=[
        CheckResult(id="activation", status="fail", required=True, duration_seconds=0)])
    activation_triage = triage_failure(failing, activation_failure)
    rewritten_from_failing = failing.model_copy(update={"activation": failing.activation.model_copy(
        update={"path": ["{cache}/other"]})})
    guard_repair_diff(rewritten_from_failing, failing, activation_triage)


def test_a_triage_for_another_manifest_is_refused():
    previous = base()
    triage = triage_of(previous)
    other = build(install=[cmd("install-tool", ["make", "different"])])

    with pytest.raises(EnvironmentDiscoveryError) as exc:
        guard_repair_diff(previous, other, triage)
    assert exc.value.code == "repair_triage_stale"
