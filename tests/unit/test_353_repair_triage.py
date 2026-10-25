"""Failure triage: the closure an execution repair may change, computed by the daemon without a model."""

from __future__ import annotations

import json
from pathlib import Path

from coordinare_service_inference.environment.models import (
    Activation,
    CheckResult,
    Command,
    EnvironmentManifest,
    Service,
    VerificationReport,
)
from coordinare_service_inference.environment.runtime import RuntimeSpec
from coordinare_service_inference.environment.static_rules import connectivity_probe

from coordinare.services.environment_repair_triage import (
    RepairTriage,
    triage_failure,
)
from tests.unit.env_manifest_fixture import reviewed_metadata

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "353_sample_install_failure.json"


def build(*, install=(), checks=(), services=(), activation=None, requirements=(), criterion_checks=None):
    """A validated manifest from small synthetic records (schema 1: no scope contract needed)."""
    data = reviewed_metadata(install=list(install), checks=list(checks) or [{"id": "baseline", "argv": ["true"]}],
                             services=list(services), activation=activation or {})["environment_manifest"]
    data["requirements"] = list(requirements)
    manifest = EnvironmentManifest.model_validate(data)
    # Scope mappings need a reviewed (schema 3) manifest; the triage reads them, so attach them unvalidated.
    return manifest.model_copy(update={"criterion_checks": dict(criterion_checks or {})})


def report(manifest, failed, *, not_run=(), indeterminate=(), output=None):
    results = []
    for identifier in failed:
        results.append(CheckResult(id=identifier, status="fail", required=True, exit_code=1, duration_seconds=1,
                                   output=(output or {}).get(identifier, "")))
    for identifier in indeterminate:
        results.append(CheckResult(id=identifier, status="indeterminate", required=True, duration_seconds=1))
    for identifier in not_run:
        results.append(CheckResult(id=identifier, status="not_run", required=True, output="template preflight did not pass"))
    results.append(CheckResult(id="passing-check", status="pass", required=True, exit_code=0, duration_seconds=1))
    return VerificationReport(manifest_digest=manifest.digest(), results=results)


def cmd(identifier, argv=("true",), **extra):
    return {"id": identifier, "argv": list(argv), **extra}


def test_single_failing_install_closes_over_its_prerequisites_and_dependents_only():
    manifest = build(
        install=[cmd("install-base"), cmd("install-tool", ["make", "tool"], depends_on=["install-base"]),
                 cmd("install-after", ["make", "after"], depends_on=["install-tool"]), cmd("install-unrelated", ["make", "other"])],
        checks=[cmd("check-tool", ["tool", "--version"]), cmd("check-other", ["other"])],
    )
    triage = triage_failure(manifest, report(manifest, ["install-tool"], not_run=["install-base", "install-after"]))

    assert triage.closure_ids == ("install-after", "install-base", "install-tool")
    assert triage.roots == {"install-tool": "command"}
    assert triage.consequences == ("install-after", "install-base")
    assert "install-unrelated" not in triage.closure_ids
    assert "check-tool" not in triage.closure_ids and "check-other" not in triage.closure_ids
    assert triage.touched_parts == ("global",)


def test_dependents_of_a_root_are_included_but_not_dependents_of_its_ancestors():
    manifest = build(
        install=[cmd("install-base", ["make", "base"]), cmd("install-a", ["make", "a"], depends_on=["install-base"]),
                 cmd("install-b", ["make", "b"], depends_on=["install-base"])],
    )
    triage = triage_failure(manifest, report(manifest, ["install-a"]))

    assert triage.closure_ids == ("install-a", "install-base")
    assert "install-b" not in triage.closure_ids


def test_service_failure_takes_its_whole_lifecycle_unit_and_its_criteria():
    service = {"id": "db", "start": cmd("db-start", ["db", "start"]), "health": cmd("db-health", ["db", "ping"]),
               "stop": cmd("db-stop", ["db", "stop"])}
    manifest = build(
        services=[service],
        checks=[cmd("c00-query", ["query"], depends_on=["db-health"]), cmd("c01-other", ["other"])],
        criterion_checks={"S0123456789ABCDEF": ["c00-query"], "S1123456789ABCDEF": ["c01-other"]},
    )
    triage = triage_failure(manifest, report(manifest, ["db-health"], not_run=["c00-query", "db-stop"]))

    assert triage.roots == {"db-health": "service"}
    assert {"db-start", "db-health", "db-stop", "c00-query"} <= set(triage.closure_ids)
    assert "c01-other" not in triage.closure_ids
    assert triage.criterion_ids == ("S0123456789ABCDEF",)
    assert triage.touched_parts == ("c00-", "global")


def test_requirements_link_to_closure_checks_and_only_those():
    requirement = {"id": "req-tool", "description": "tool works", "source": "tools.plan", "checks": ["check-tool"]}
    other = {"id": "req-other", "description": "other works", "source": "tools.plan", "checks": ["check-other"]}
    manifest = build(
        install=[cmd("install-tool", ["make", "tool"])],
        checks=[cmd("check-tool", ["tool"]), cmd("check-other", ["other"])],
        requirements=[requirement, other],
    )
    triage = triage_failure(manifest, report(manifest, ["check-tool"]))

    assert triage.requirement_ids == ("req-tool",)
    assert "check-tool" in triage.closure_ids and "check-other" not in triage.closure_ids


def test_roots_are_classified_by_what_failed():
    manifest = build(
        install=[cmd("install-probe", ["bash", "-c", "curl -I https://example.invalid >/dev/null"]),
                 cmd("install-helm", ["bash", "-c", "curl -fsSL https://example.invalid/helm.tgz -o /tmp/h"]),
                 cmd("install-binary", ["bash", "-c", "tool-x"])],
        checks=[cmd("check-plain", ["plain"])],
        services=[{"id": "docker", "start": cmd("docker-start", ["dockerd"]), "health": cmd("docker-health", ["docker", "info"]),
                   "stop": cmd("docker-stop", ["docker", "stop"])}],
        activation={"path": ["{cache}/bin"]},
    )
    triage = triage_failure(manifest, report(
        manifest, ["install-probe", "install-binary", "docker-start", "check-plain"],
        indeterminate=[], output={"install-binary": "bash: dockerd: not found", "docker-start": "boom"}))

    assert triage.roots["install-probe"] == "network_gate"
    assert triage.roots["install-binary"] == "capability"
    assert triage.roots["docker-start"] == "service"
    assert triage.roots["check-plain"] == "command"
    assert connectivity_probe(manifest.install[0]) and not connectivity_probe(manifest.install[1])


def test_activation_failure_is_an_activation_path_root():
    manifest = build(install=[cmd("install-bin", ["make", "bin"])], activation={"path": ["{cache}/bin"]})
    triage = triage_failure(manifest, report(manifest, [], indeterminate=[]).model_copy(update={"results": [
        CheckResult(id="activation", status="fail", required=True, duration_seconds=0)]}))

    assert triage.roots == {"activation": "activation_path"}
    # Activation paths are created by installs, which no depends_on edge names: every install is a candidate producer.
    assert triage.closure_ids == ("activation", "install-bin")
    assert triage.touched_parts == ("global",)


def test_a_failed_check_brings_every_install_in_as_a_candidate_producer():
    """Clean checks cannot depend on install commands, so the producer of what a check reads is not named by depends_on."""
    manifest = build(
        install=[cmd("install-base", ["make", "base"]), cmd("install-payload", ["make", "payload"]),
                 cmd("install-unused", ["make", "unused"])],
        checks=[cmd("check-payload", ["check", "payload"]), cmd("check-other", ["other"])],
    )
    triage = triage_failure(manifest, report(manifest, ["check-payload"]))

    assert triage.roots == {"check-payload": "command"}
    assert set(triage.closure_ids) == {"check-payload", "install-base", "install-payload", "install-unused"}
    assert "check-other" not in triage.closure_ids


def test_indeterminate_results_are_roots_too():
    manifest = build(install=[cmd("install-a", ["make"])])
    triage = triage_failure(manifest, report(manifest, [], indeterminate=["install-a"]))

    assert triage.roots == {"install-a": "command"}


def test_digest_is_stable_and_changes_with_the_closure_records():
    manifest = build(install=[cmd("install-a", ["make", "a"]), cmd("install-b", ["make", "b"], depends_on=["install-a"])])
    first = triage_failure(manifest, report(manifest, ["install-a"]))
    again = triage_failure(manifest, report(manifest, ["install-a"]))
    changed = build(install=[cmd("install-a", ["make", "c"]), cmd("install-b", ["make", "b"], depends_on=["install-a"])])
    other = triage_failure(changed, report(changed, ["install-a"]))

    assert first.digest == again.digest and len(first.digest) == 64
    assert other.digest != first.digest
    assert RepairTriage.model_validate_json(first.model_dump_json()) == first


def test_no_failure_is_not_a_triage():
    manifest = build(install=[cmd("install-a", ["make"])])
    try:
        triage_failure(manifest, VerificationReport(manifest_digest=manifest.digest(),
                                                    results=[CheckResult(id="install-a", status="pass", required=True)]))
    except ValueError as exc:
        assert "no failed" in str(exc)
    else:
        raise AssertionError("a triage needs a failed result")


def test_sample_install_replay_closes_over_the_failing_helm_install_only():
    """The sample first generation: one failing command (install-helm-gh), everything else preflight-stopped."""
    data = json.loads(FIXTURE.read_text())
    records = data["manifest"]
    # Only the records are validated one by one; the whole reviewed contract (a 134 KB scope) is not needed to triage.
    manifest = EnvironmentManifest.model_construct(
        **{key: value for key, value in records.items() if key not in {"install", "checks", "services", "requirements", "activation", "runtime"}},
        install=[Command.model_validate(item) for item in records["install"]],
        checks=[Command.model_validate(item) for item in records["checks"]],
        services=[Service.model_validate(item) for item in records["services"]],
        requirements=[],
        activation=Activation.model_validate(records["activation"]),
        runtime=RuntimeSpec.model_validate(records["runtime"]) if records["runtime"] is not None else None,
    )
    run = VerificationReport.model_validate(dict(data["report"]))
    triage = triage_failure(manifest, run)

    assert triage.roots == {"install-helm-gh": "command"}
    assert triage.closure_ids == ("install-apt-prereqs", "install-helm-gh")
    assert triage.touched_parts == ("global",)
    assert triage.criterion_ids == () and triage.requirement_ids == ()
    assert len(triage.consequences) == sum(1 for result in run.results if result.status == "not_run")
    assert len(triage.closure_ids) <= 3
