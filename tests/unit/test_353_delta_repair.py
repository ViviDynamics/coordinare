"""Delta repair: a failed execution's repair re-authors only its closure, as changed and added records."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from coordinare_service_inference.environment import models as env
from coordinare_service_inference.environment.findings import FindingLedger
from coordinare_service_inference.environment.static_rules import connectivity_probe
from pydantic import ValidationError

from coordinare.services import environment_discovery as discovery_module
from coordinare.services import environment_shard_construction as construction_module
from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_delta import DeltaPlan, changed_ids
from coordinare.services.environment_delta_schema import (
    ManifestDelta,
    apply_delta,
    apply_group_delta,
)
from coordinare.services.environment_discovery import DiscoveryLimits, _delta_repair
from coordinare.services.environment_obligations import guard_repair_diff
from coordinare.services.environment_probes import select_current_probe_reports
from coordinare.services.environment_repair_triage import triage_failure
from coordinare.services.environment_shard_construction import (
    CheckGroup,
    DividedConstruction,
    validate_group,
)
from coordinare.services.environment_shard_scope import AgentContext
from tests.unit.test_351_environment_discovery import CAPABILITIES

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "353_sample_install_failure.json"
COMMIT = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def load_gen0() -> tuple[env.EnvironmentManifest, env.VerificationReport]:
    """The sample installation failure. Its scope contract is not stored in the fixture, so records are typed and the manifest is
    built without the schema-3 scope validator; triage and admission read only the records."""
    data = json.loads(FIXTURE.read_text())
    raw = data["manifest"]
    typed = {"checks": [env.Command.model_validate(c) for c in raw["checks"]],
             "install": [env.Command.model_validate(c) for c in raw["install"]],
             "services": [env.Service.model_validate(s) for s in raw["services"]],
             "activation": env.Activation.model_validate(raw["activation"]),
             "requirements": [env.Requirement.model_validate(r) for r in raw["requirements"]]}
    manifest = env.EnvironmentManifest.model_construct(**{**raw, **typed})
    report = env.VerificationReport.model_validate({k: v for k, v in data["report"].items() if k != "passed"})
    return manifest, report


def as_manifest(proposal: dict[str, Any], base: env.EnvironmentManifest) -> env.EnvironmentManifest:
    """A manifest for a merged proposal dict, typed like the base; the non-proposal fields come from the base."""
    typed = {"checks": [env.Command.model_validate(c) for c in proposal["checks"]],
             "install": [env.Command.model_validate(c) for c in proposal["install"]],
             "services": [env.Service.model_validate(s) for s in proposal["services"]],
             "activation": env.Activation.model_validate(proposal["activation"]),
             "requirements": [env.Requirement.model_validate(r) for r in proposal["requirements"]]}
    data = {**base.model_dump(mode="json", exclude=set(typed) | {"criterion_checks"}), **proposal}
    return env.EnvironmentManifest.model_construct(**{**data, **typed, "criterion_checks": proposal.get("criterion_checks", {})})


class FakeCriterion(SimpleNamespace):
    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return {"id": self.id, "description": self.description, "applicability": self.applicability, "sources": self.sources}


def fixture_groups(manifest: env.EnvironmentManifest) -> tuple[list[CheckGroup], list[FakeCriterion]]:
    """Criteria as the fixture maps them, grouped by the check prefix they map to (c00 before c01)."""
    criteria = []
    for index, (identifier, mapped) in enumerate(sorted(manifest.criterion_checks.items())):
        prefix = "c01-" if mapped and all(item.startswith("c01-") for item in mapped if item.startswith("c0")) else "c00-"
        criteria.append(FakeCriterion(id=identifier, description=f"criterion {index}", applicability="required",
                                      sources=[sorted(manifest.inputs)[0]], prefix=prefix))
    groups = [CheckGroup(prefix, tuple(c for c in criteria if c.prefix == prefix)) for prefix in ("c00-", "c01-")]
    return [group for group in groups if group.criteria], criteria


def delta_for(manifest: env.EnvironmentManifest, identifier: str, **changes: Any) -> dict[str, Any]:
    """The delta an author returns: the failed record with the changes applied, and nothing else."""
    record = next(item for item in manifest.install if item.id == identifier)
    return {"install": [{**record.model_dump(mode="json"), **changes}]}


def cmd(identifier: str, argv: list[str], **extra: Any) -> dict[str, Any]:
    return {"id": identifier, "argv": argv, **extra}


# --- pure record semantics -------------------------------------------------------------------------------------------

def base_proposal() -> dict[str, Any]:
    return {"requirements": [], "install": [cmd("install-a", ["make", "a"]), cmd("install-b", ["make", "b"])],
            "checks": [cmd("check-a", ["a"])], "services": [], "activation": {"path": [], "env": {}},
            "criterion_checks": {"S1": ["check-a"]}, "runtime_endpoints": [], "empty_reason": None, "local_credentials": []}


def test_a_change_replaces_its_base_record_and_omitted_records_carry_over():
    merged = apply_delta(base_proposal(), {"install": [cmd("install-a", ["make", "a", "PREFIX=/opt"])]})

    assert [item["id"] for item in merged["install"]] == ["install-a", "install-b"]
    assert merged["install"][0]["argv"] == ["make", "a", "PREFIX=/opt"]
    assert merged["install"][1] == base_proposal()["install"][1]
    assert merged["checks"] == base_proposal()["checks"]


def test_an_added_record_joins_and_a_duplicate_id_is_never_silently_chosen():
    added = apply_delta(base_proposal(), {"install": [cmd("install-c", ["make", "c"])]})
    assert [item["id"] for item in added["install"]] == ["install-a", "install-b", "install-c"]

    duplicated = apply_delta(base_proposal(), {"install": [cmd("install-a", ["x"]), cmd("install-a", ["y"])]})
    assert [item["id"] for item in duplicated["install"]].count("install-a") == 2


def test_criterion_mappings_merge_per_criterion_and_activation_replaces_only_when_given():
    merged = apply_delta(base_proposal(), {"criterion_checks": {"S2": ["check-a"]}})
    assert merged["criterion_checks"] == {"S1": ["check-a"], "S2": ["check-a"]}
    assert merged["activation"] == base_proposal()["activation"]
    assert apply_delta(base_proposal(), {"activation": {"path": ["{cache}/bin"], "env": {}}})["activation"]["path"] == ["{cache}/bin"]


def test_a_check_author_patch_applies_to_its_part_and_keeps_unlisted_checks():
    base = {"checks": [cmd("c00-a", ["a"]), cmd("c00-b", ["b"])], "criterion_checks": {"S1": ["c00-a"], "S2": ["c00-b"]}}
    applied = apply_group_delta(base, {"checks": [cmd("c00-b", ["b", "--strict"])], "criterion_checks": {"S2": ["c00-b"]}})

    assert [check["id"] for check in applied["checks"]] == ["c00-a", "c00-b"]
    assert applied["checks"][1]["argv"] == ["b", "--strict"]
    assert applied["criterion_checks"] == {"S1": ["c00-a"], "S2": ["c00-b"]}


def test_the_delta_envelope_rejects_fields_it_does_not_own():
    with pytest.raises(ValidationError):
        ManifestDelta.model_validate({"install": [], "empty_reason": "nope"})
    assert ManifestDelta.model_validate({"install": [cmd("install-a", ["make"])]}).install[0].id == "install-a"


def test_changed_ids_names_changed_added_and_removed_records():
    base = load_gen0()[0]
    data = base.model_dump(mode="json")
    data["install"] = [item for item in data["install"] if item["id"] != "install-npm"]
    for item in data["install"]:
        if item["id"] == "install-ruby":
            item["argv"] = ["make", "ruby", "-j1"]
    data["install"].append(cmd("install-zzz", ["make", "zzz"]))
    candidate = as_manifest(data, base).model_dump(mode="json")

    assert changed_ids(base, candidate) == {"changed": ["install-ruby"], "added": ["install-zzz"], "removed": ["install-npm"]}


# --- triage ---------------------------------------------------------------------------------------------------------

def test_the_sample_install_failure_triages_to_its_install_and_prerequisite_only():
    manifest, report = load_gen0()
    triage = triage_failure(manifest, report)

    assert triage.roots == {"install-helm-gh": "command"}
    assert triage.closure_ids == ("install-apt-prereqs", "install-helm-gh")
    assert triage.touched_parts == ("global",)
    assert triage.manifest_digest == manifest.digest()


def test_a_capability_install_root_brings_every_install_into_the_closure():
    base = load_gen0()[0]
    install_ids = [command.id for command in base.install]
    failing = next(command.id for command in base.install if command.id == "install-helm-gh")
    report = env.VerificationReport(manifest_digest=base.digest(), results=[
        env.CheckResult(id=failing, status="fail", required=True, exit_code=127, duration_seconds=1,
                      output="helm: command not found")])
    triage = triage_failure(base, report)

    assert triage.roots == {failing: "capability"}
    assert set(install_ids) <= set(triage.closure_ids)


def test_the_classifier_is_the_shared_static_rule():
    import coordinare.services.environment_repair_triage as triage_module

    gate = env.Command.model_validate(cmd("gate", ["bash", "-c", "curl -fsI https://example.invalid >/dev/null"]))
    assert connectivity_probe(gate) and not connectivity_probe(load_gen0()[0].install[0])
    assert not hasattr(triage_module, "is_connectivity_gate")


# --- probe reuse ----------------------------------------------------------------------------------------------------

def probe_report(capabilities: env.ExecutionCapabilities, *, command_argv: list[str]) -> dict[str, Any]:
    """A saved, completed probe report whose plan was taken under ``capabilities`` and runs ``command_argv``."""
    from coordinare.services.environment_probes import ProbePlan, ProbeReport, ProbeRequest

    request = ProbeRequest.model_validate({"commands": [{"id": "probe-tool", "argv": command_argv, "required": True,
        "scope": [], "depends_on": [], "timeout_seconds": 10}],
        "citations": {"probe-tool": ["src/tool.md"]}, "questions": {"probe-tool": "is the tool present"}})
    plan = ProbePlan(request=request, source_commit=COMMIT, inventory_digest="a" * 64, execution_source_digest="e" * 64,
                     inputs={"src/tool.md": "b" * 64}, capabilities=capabilities)
    report = ProbeReport(schema_version=1, plan_digest=plan.digest(), source_commit=COMMIT, image_ref=capabilities.image_ref,
                         platform=capabilities.platform, execution_source_digest="e" * 64,
                         results=[{"id": "probe-tool", "status": "completed", "exit_code": 0, "output": "", "truncated": False}],
                         cleanup_confirmed=True, plan=plan)
    return report.model_dump(mode="json")


def capabilities(cache: str) -> env.ExecutionCapabilities:
    return CAPABILITIES.model_copy(update={"cache_mount_path": cache})


def test_a_cache_only_relocation_keeps_observations_no_probe_command_reads():
    old, new = capabilities("/cache/old"), capabilities("/cache/new")
    reports = [probe_report(old, command_argv=["tool", "--version"])]

    assert select_current_probe_reports(reports, COMMIT, new, allow_cache_relocation=True, reuse_unreferenced=True) == reports
    assert select_current_probe_reports(reports, COMMIT, new, allow_cache_relocation=True) == []


def test_a_probe_that_names_the_cache_is_retired_and_a_capability_root_forces_a_fresh_batch():
    old, new = capabilities("/cache/old"), capabilities("/cache/new")
    referencing = [probe_report(old, command_argv=["ls", "/cache/old/bin"])]
    templated = [probe_report(old, command_argv=["ls", "{cache}/bin"])]

    assert select_current_probe_reports(referencing, COMMIT, new, allow_cache_relocation=True, reuse_unreferenced=True) == []
    assert select_current_probe_reports(templated, COMMIT, new, allow_cache_relocation=True, reuse_unreferenced=True) == []


def test_the_delta_switch_turns_delta_repair_off_and_defaults_to_on():
    budget = AttemptBudget()
    budget.repair_triage = object()  # the switch is read before any triage is used
    options = SimpleNamespace(previous_manifest=object(), execution_feedback={"results": []})

    assert DiscoveryLimits().delta_repair is True
    assert _delta_repair(options, DiscoveryLimits(), budget) is True
    assert _delta_repair(options, DiscoveryLimits(delta_repair=False), budget) is False
    budget.delta_fallback = True
    assert _delta_repair(options, DiscoveryLimits(), budget) is False


# --- replay of the sample installation failure ---------------------------------------------------------------------------------

class Counter:
    def __init__(self) -> None:
        self.calls: dict[str, int] = {"global_author": 0, "group_author": 0, "review": 0}
        self.review_messages: list[list[dict[str, str]]] = []


class FakeBuild:
    """Authors a group on request, as the real divided construction does, and counts each request."""

    def __init__(self, groups: list[CheckGroup], base: env.EnvironmentManifest, counter: Counter) -> None:
        self.groups = groups
        self.base = base
        self.counter = counter
        self.feedback: dict = {}
        self.previous: dict = {}
        self.group_inputs: dict = {}
        self.global_part = None

    async def author_all(self, *, summary, probes, groups=None):
        asked = list(self.groups if groups is None else groups)
        self.counter.calls["group_author"] += len(asked)
        # A full repair re-authors each group; this stub returns each group's failed part, as a no-change author would.
        parts = {}
        for group in asked:
            part = {"checks": [c.model_dump(mode="json") for c in self.base.checks if c.id.startswith(group.prefix)],
                    "criterion_checks": {k: v for k, v in self.base.criterion_checks.items() if k in group.ids}}
            parts[group.prefix] = (group, validate_group({"shard_checks": part}, group=group, summary=summary))
        return parts


def discovery_stub() -> SimpleNamespace:
    async def bind_verified(expected, **_kwargs):
        return [{"path": path} for path in expected]

    return SimpleNamespace(inputs={}, finding_responses=[], bind_verified=bind_verified, name="discovery")


def replay_attempt(budget: AttemptBudget, plan: DeltaPlan | None, messages: list) -> SimpleNamespace:
    async def observe(_inputs):
        return None

    messages = messages or [{'role': 'system', 'content': 'Synthetic author'}, {'role': 'user', 'content': '{}'}]
    return SimpleNamespace(budget=budget, required_ids={}, obligation_ledger=SimpleNamespace(coverage={}, checks={}), delta=plan, discovery=discovery_stub(), messages=messages, chat=None,
                           construction=SimpleNamespace(observe_sources=observe), prior_manifests=[plan.base] if plan else [])


def gen0_budget(triage) -> AttemptBudget:
    budget = AttemptBudget(max_calls=1024, max_reads=8192, max_coverage_revisions=8, max_schema_repairs=8)
    budget.repair_triage = triage
    budget.repair_delta = True
    budget.finding_ledger = FindingLedger(source_commit=COMMIT)
    return budget


@pytest.fixture
def replay():
    """The sample installation failure: its manifest, triage and a call counter shared by the stubs of one test."""
    manifest, report = load_gen0()
    return SimpleNamespace(manifest=manifest, triage=triage_failure(manifest, report), counter=Counter())


@pytest.mark.asyncio
async def test_replay_sample_delta_authors_only_the_closure_and_adds_no_duplicate_check(replay, monkeypatch):
    manifest, triage, counter = replay.manifest, replay.triage, replay.counter
    plan = DeltaPlan(manifest, triage)
    changed = delta_for(manifest, "install-helm-gh", argv=["bash", "-c", "set -e; ver=3.17.0; curl -fsSL https://get.helm.sh/helm-$ver.tgz -o /tmp/h.tgz"])
    groups, _criteria = fixture_groups(manifest)
    build = FakeBuild(groups, manifest, counter)
    budget = gen0_budget(triage)
    messages = [{"role": "system", "content": ""}, {"role": "user", "content": "{}"}]
    attempt = replay_attempt(budget, plan, messages)

    async def phase(*, inspection, messages, chat, budget, validate):
        counter.calls["global_author"] += 1
        validate(changed)  # the real admission callback runs on the delta before the merge
        return changed, {}

    monkeypatch.setattr(discovery_module, "_phase", phase)
    candidate = await discovery_module._divided_candidate(build, attempt, lambda data: data)
    merged = as_manifest(candidate, manifest)

    # Only the touched part was authored: the global author, and no check group (the closure is global).
    assert counter.calls == {"global_author": 1, "group_author": 0, "review": 0}
    # The candidate differs from the failed accepted manifest by one changed record, and adds nothing.
    assert changed_ids(manifest, candidate) == {"changed": ["install-helm-gh"], "added": [], "removed": []}
    assert [c.model_dump(mode="json") for c in merged.checks] == [c.model_dump(mode="json") for c in manifest.checks]
    assert len(candidate["checks"]) == len(manifest.checks)
    # Admission of the closure: the guard passes for the delta and names the frozen record for an out-of-closure edit.
    guard_repair_diff(merged, manifest, triage)
    outside = copy.deepcopy(candidate)
    outside["checks"][-1]["argv"] = ["make", "changed-outside"]  # checks are outside every install closure
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        guard_repair_diff(as_manifest(outside, manifest), manifest, triage)
    assert caught.value.code == "repair_outside_closure"


@pytest.mark.asyncio
async def test_replay_sample_full_path_authors_every_part_for_comparison(replay, monkeypatch):
    manifest, triage, counter = replay.manifest, replay.triage, replay.counter
    groups, _criteria = fixture_groups(manifest)
    build = FakeBuild(groups, manifest, counter)
    budget = gen0_budget(triage)
    budget.repair_delta = False
    attempt = replay_attempt(budget, None, [{"role": "system", "content": ""}, {"role": "user", "content": "{}"}])

    # The full path re-authors the global part and every check group, as a whole part each time.
    global_full = {k: v for k, v in manifest.model_dump(mode="json").items() if k in
                   {"requirements", "install", "activation", "services", "checks", "criterion_checks", "runtime_endpoints",
                    "empty_reason", "local_credentials"}}
    global_full["checks"] = [c for c in global_full["checks"] if not c["id"].startswith(("c00-", "c01-"))]
    global_full["criterion_checks"] = {}

    async def phase_full(**kwargs):
        counter.calls["global_author"] += 1
        return global_full, {}

    monkeypatch.setattr(discovery_module, "_phase", phase_full)
    await discovery_module._divided_candidate(build, attempt, lambda data: data)
    assert counter.calls == {"global_author": 1, "group_author": len(groups), "review": 0}


def assessment(verdict: str, sources: list[str] | None = None) -> env.CoverageAssessmentProposal:
    return env.CoverageAssessmentProposal.model_validate({"findings": [], "decisions": [], "capability_unavailable": False,
                                                        "verdict": verdict, "rationale": "judged", "sources": sources or ["a.md"]})


class FreshInspection:
    def __init__(self, *_args: Any) -> None:
        self.inputs: dict[str, str] = {}

    async def bind_verified(self, expected, **_kwargs):
        self.inputs.update(expected)
        return [{"path": path} for path in expected]


@pytest.mark.asyncio
async def test_replay_sample_review_judges_only_the_changed_part_and_carries_the_rest(replay, monkeypatch):
    manifest, triage, counter = replay.manifest, replay.triage, replay.counter
    plan = DeltaPlan(manifest, triage)
    groups, _criteria = fixture_groups(manifest)
    budget = gen0_budget(triage)
    budget.repair_delta = False
    base_attempt = SimpleNamespace(budget=budget, required_ids={}, obligation_ledger=SimpleNamespace(coverage={}, checks={}), delta=None, identity={"symphony_name": "suite"},
                                   discovery=SimpleNamespace(finding_responses=[]))
    build = SimpleNamespace(groups=groups, scope=SimpleNamespace(definition=SimpleNamespace(criteria=[]), finding_ledger=None),
                            context=None, concurrency=4)
    reviewer = SimpleNamespace(entries=None, paths=[], fetch=None, limits=None, inputs={})
    seen: list[dict] = []

    async def agent(_context, inspection, messages, task):
        counter.calls["review"] += 1
        document = json.loads(messages[1]["content"])
        shared, own = document["shared"], document["own"]
        seen.append({"own": own, "shared": shared})
        # As the real runner does: the reply goes through the task's validation, rulings included when a scope is present.
        rulings = [{"criterion_id": item, "verdict": "accept", "rationale": "covered"}
                   for item in own.get("required_criterion_ids", [])]
        reply = {"assessment": {**assessment("accept", sorted(inspection.inputs)).model_dump(mode="json"),
                                "criterion_rulings": rulings}}
        return task.validate(reply)

    monkeypatch.setattr(discovery_module, "_Inspection", FreshInspection)
    monkeypatch.setattr(discovery_module, "run_agent", agent)
    monkeypatch.setattr(discovery_module, "manifest_obligation_ids", lambda _manifest: set())
    await discovery_module._divided_review(build, base_attempt, manifest, reviewer)  # the reviewed failed manifest
    assert counter.calls["review"] == len(groups) + 1

    changed = apply_delta(manifest.model_dump(mode="json", include={"checks", "install", "requirements", "services", "activation",
                                                                    "criterion_checks", "runtime_endpoints", "empty_reason",
                                                                    "local_credentials"}),
                          delta_for(manifest, "install-helm-gh", argv=["bash", "-c", "true"]))
    candidate = as_manifest(changed, manifest)
    before = counter.calls["review"]
    seen.clear()
    delta_attempt = SimpleNamespace(budget=budget, required_ids={}, obligation_ledger=SimpleNamespace(coverage={}, checks={}), delta=plan, identity={"symphony_name": "suite"},
                                    discovery=SimpleNamespace(finding_responses=[]))
    budget.repair_delta = True
    await discovery_module._divided_review(build, delta_attempt, candidate, reviewer)

    # The unchanged groups keep their reviewed standing; only the global part is judged again, with its repair scope.
    assert counter.calls["review"] - before == 1
    [judged] = seen
    assert judged["own"]["repair_scope"]["changed"] == ["install-helm-gh"]
    assert judged["own"]["repair_scope"]["added"] == []
    assert "untrusted_execution_history" in judged["shared"]


# --- escalation, restart, and author shape --------------------------------------------------------------------------

async def _no_chat(_messages):
    return "{}"


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["repair_delta_rejected", "attempt_schema_limit"])
async def test_a_delta_that_cannot_be_admitted_or_reviewed_falls_back_to_the_full_repair(monkeypatch, code):
    seen: list[tuple[bool, bool]] = []
    budget = AttemptBudget()
    budget.repair_triage = triage_failure(*load_gen0())

    async def attempt(**kwargs):
        options = kwargs["options"]
        seen.append((options.budget.repair_delta, options.budget.delta_fallback))
        if len(seen) == 1:
            raise EnvironmentDiscoveryError("delta rejected", code=code)
        return "manifest", "assessment"

    monkeypatch.setattr(discovery_module, "_discover_attempt", attempt)
    monkeypatch.setattr(discovery_module, "_delta_repair", lambda *_args: True)
    monkeypatch.setattr(discovery_module, "select_current_probe_reports", lambda reports, *_args, **_kw: reports)
    result = await discovery_module.discover_and_review(
        symphony_name="suite", commit=COMMIT, paths=[], fetch=None, chat=_no_chat, budget=budget,
        capabilities=capabilities("/cache/new"), previous_manifest=object(), execution_feedback={"results": []})

    assert result == ("manifest", "assessment")
    assert seen == [(True, False), (False, True)]
    assert budget.delta_fallback is True and budget.repair_delta is False


@pytest.mark.asyncio
async def test_a_restart_reuses_the_saved_delta_and_makes_no_author_call(replay, monkeypatch):
    manifest, triage, counter = replay.manifest, replay.triage, replay.counter
    plan = DeltaPlan(manifest, triage)
    changed = delta_for(manifest, "install-helm-gh", argv=["bash", "-c", "true"])
    groups, _criteria = fixture_groups(manifest)
    first = gen0_budget(triage)
    phases: list[int] = []

    async def phase(*, inspection, messages, chat, budget, validate):
        phases.append(1)
        validate(changed)
        return changed, {}

    monkeypatch.setattr(discovery_module, "_phase", phase)
    await discovery_module._divided_candidate(FakeBuild(groups, manifest, counter), replay_attempt(first, plan, []), lambda d: d)
    assert phases == [1]

    # The journal stores the parts as JSON; a restarted budget restores them and resumes the same triage digest.
    restored = gen0_budget(triage_from_journal(triage))
    restored.executable_parts = json.loads(json.dumps(first.executable_parts))
    await discovery_module._divided_candidate(FakeBuild(groups, manifest, counter), replay_attempt(restored, plan, []), lambda d: d)
    assert phases == [1]


def triage_from_journal(triage):
    from coordinare.services.environment_repair_triage import RepairTriage

    return RepairTriage.model_validate(json.loads(triage.model_dump_json()))


@pytest.mark.asyncio
async def test_a_touched_group_is_authored_as_a_patch_applied_to_its_failed_part(monkeypatch):
    """A check group the failure touches returns only its changed check; the other checks come from its failed part."""
    base = as_manifest(base_proposal(), load_gen0()[0]).model_copy(update={
        "checks": [env.Command.model_validate(cmd("c00-a", ["a"])), env.Command.model_validate(cmd("c00-b", ["b"]))],
        "criterion_checks": {"S1": ["c00-a"], "S2": ["c00-b"]}})
    criteria = [FakeCriterion(id="S1", description="one", applicability="required", sources=["src/1.md"]),
                FakeCriterion(id="S2", description="two", applicability="required", sources=["src/2.md"])]
    triage = SimpleNamespace(digest="d" * 64, closure_ids=("c00-b",), criterion_ids=("S2",), touched_parts=("c00-",),
                             requirement_ids=(), roots={"c00-b": "command"})
    plan = DeltaPlan(base, triage)
    group = CheckGroup("c00-", tuple(criteria))

    class StubInspection:
        def __init__(self, *_args: Any) -> None:
            self.inputs: dict[str, str] = {}
            self.name = ""

        async def bind_verified(self, expected, **_kwargs):
            self.inputs.update(expected)
            return []

    async def agent(_context, _inspection, _messages, task):
        reply = {"shard_checks": {"checks": [cmd("c00-b", ["b", "--strict"])], "criterion_checks": {"S2": ["c00-b"]}}}
        return task.validate(reply)

    monkeypatch.setattr(construction_module, "SourceInspection", StubInspection)
    monkeypatch.setattr(construction_module, "run_agent", agent)
    budget = AttemptBudget(max_calls=8, max_reads=8)
    scope = SimpleNamespace(definition=SimpleNamespace(criteria=criteria), finding_ledger=FindingLedger(source_commit=COMMIT),
                            all_inputs={"src/1.md": "a" * 64, "src/2.md": "b" * 64})
    previous = {"c00-": {"checks": [c.model_dump(mode="json") for c in base.checks], "criterion_checks": dict(base.criterion_checks)}}
    build = DividedConstruction(AgentContext(budget, DiscoveryLimits(), None), {}, None, scope, 1, previous=previous, delta=plan)
    summary = {"services": [], "readiness_ids": [], "checks": [], "install_ids": [], "activation": {}, "local_credentials": [],
               "runtime_endpoints": []}
    part = await build.author_delta(group, summary=summary, probes=[])

    assert [check.id for check in part.checks] == ["c00-a", "c00-b"]
    assert part.checks[1].argv == ["b", "--strict"]
    assert part.criterion_checks == {"S1": ["c00-a"], "S2": ["c00-b"]}
    saved = budget.executable_parts["delta"]["c00-"]
    assert saved["key"] == "d" * 64
    assert [check["id"] for check in saved["data"]["shard_checks"]["checks"]] == ["c00-b"]


# --- end to end through the real discovery entry point ---------------------------------------------------------------

async def delta_run(chat, *, budget):
    """A repair of a validated previous manifest whose check ``version`` failed, through discover_and_review."""
    from coordinare_service_inference.environment.models import CheckResult, VerificationReport

    from tests.unit.scope_chat_fixture import discover_with_fixture_scope
    from tests.unit.test_351_environment_discovery import candidate as previous_candidate
    from tests.unit.test_353_discovery_repair import CAPABILITIES, COMMIT, PATH, SOURCE

    files = {PATH: SOURCE, **{f"evidence/{i}": f"required evidence {i}" for i in range(17)}}
    previous_data = previous_candidate()
    previous_data["inputs"] = {path: hashlib.sha256(text.encode()).hexdigest() for path, text in files.items()}
    previous_data["inventory_digest"] = hashlib.sha256(json.dumps(sorted(files), separators=(",", ":")).encode()).hexdigest()
    previous_data["checks"].append({"id": "other", "argv": ["zog", "--other"], "required": False})
    previous = env.EnvironmentManifest.model_validate(previous_data)
    failed = VerificationReport(manifest_digest=previous.digest(), results=[
        CheckResult(id="version", status="fail", required=True, exit_code=1, duration_seconds=1, output="zog: failed")])
    budget.repair_triage = triage_failure(previous, failed)

    async def fetch(path):
        return files.get(path)

    return await discover_with_fixture_scope(symphony_name="suite", commit=COMMIT, paths=sorted(files),
        capabilities=CAPABILITIES, fetch=fetch, chat=chat, budget=budget, previous_manifest=previous,
        execution_feedback={"failed": True})


@pytest.mark.asyncio
async def test_an_accepted_delta_repair_reaches_the_author_without_the_previous_manifest():
    from tests.unit.test_353_discovery_repair import assessment, candidate

    contexts: list[dict] = []

    async def chat(messages):
        context = json.loads(messages[1]["content"])
        if "manifest" in context:
            return json.dumps(assessment(candidate()))
        contexts.append(context)
        # The added mapping is the only change: the scope's one required criterion now maps the failed check.
        mapping = {criterion["id"]: ["version"] for criterion in context["scope_contract"]["definition"]["criteria"]}
        return json.dumps({"manifest_delta": {"criterion_checks": mapping}, "finding_responses": []})

    budget = AttemptBudget()
    await delta_run(chat, budget=budget)

    [author] = contexts
    assert "previous_manifest" not in author and "repair_delta" in author
    assert author["repair_delta"]["touched_parts"] == ["global"]
    # The failed check and the requirement linked to it are the closure records; nothing else is shown.
    assert sorted(author["repair_delta"]["closure_records"]) == ["tool", "version"]
    assert budget.repair_delta is True and budget.delta_fallback is False


@pytest.mark.asyncio
async def test_a_delta_that_stays_outside_its_closure_falls_back_to_the_full_repair():
    from tests.unit.test_353_discovery_repair import assessment, candidate

    kinds: list[str] = []

    async def chat(messages):
        context = json.loads(messages[1]["content"])
        if "manifest" in context:
            return json.dumps(assessment(candidate()))
        if "repair_delta" in context:
            kinds.append("delta")
            # 'other' is outside the failure's closure: admission rejects it on every correction.
            mapping = {criterion["id"]: ["version"] for criterion in context["scope_contract"]["definition"]["criteria"]}
            return json.dumps({"manifest_delta": {"criterion_checks": mapping, "checks": [
                {"id": "other", "argv": ["zog", "--changed"], "required": False}]}, "finding_responses": []})
        kinds.append("full")
        assert "previous_manifest" in context
        # The full repair is the backstop: the closure guard does not apply, so 'other' may change here.
        full = candidate()
        full["checks"].append({"id": "other", "argv": ["zog", "--changed"], "required": False})
        return json.dumps({"manifest": full, "finding_responses": []})

    budget = AttemptBudget()
    manifest, _review = await delta_run(chat, budget=budget)

    assert kinds[-1] == "full" and kinds.count("delta") >= 1
    assert budget.delta_fallback is True and budget.repair_delta is False
    assert [check.argv for check in manifest.checks if check.id == "other"] == [["zog", "--changed"]]


def test_a_delta_fallback_is_persisted_on_the_journal_and_a_restart_keeps_the_full_repair(tmp_path):
    from coordinare.services.environment_repair import begin_attempt, prepare_repair, save_attempt
    from tests.unit.test_353_repair_triage_journal import failed_first_generation

    cache = failed_first_generation(tmp_path)
    prepare_repair(cache)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=120)
    assert budget.delta_fallback is False

    budget.delta_fallback = True
    save_attempt(cache, budget)
    restarted = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=121)

    assert restarted.delta_fallback is True
    assert not _delta_repair(SimpleNamespace(previous_manifest=object(), execution_feedback={"results": []}),
                             DiscoveryLimits(), restarted)


def test_the_delta_switch_is_a_config_setting_that_defaults_on():
    from coordinare.config import EnvCacheConfig

    assert EnvCacheConfig().manifest_delta_repair is True
    assert EnvCacheConfig(manifest_delta_repair=False).manifest_delta_repair is False
