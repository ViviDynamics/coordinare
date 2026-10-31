"""Review fixes for delta repair: split groups, dependency standing, duplicate remaps, probe fallback, triage, rulings."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment import models as env
from coordinare_service_inference.environment.findings import FindingLedger
from pydantic_core import PydanticCustomError

from coordinare.services import environment_discovery as discovery_module
from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_delta import DeltaPlan, changed_ids, dependency_digest
from coordinare.services.environment_obligations import (
    duplicate_aliases,
    guard_repair_diff,
    preserve_obligations,
)
from coordinare.services.environment_repair import journal_for, prepare_repair, repair_triage_for
from coordinare.services.environment_repair_triage import RepairTriage, triage_failure
from coordinare.services.environment_shard_construction import (
    CheckGroup,
    ReviewIds,
    validate_review_part,
)
from tests.unit.test_353_delta_repair import load_gen0
from tests.unit.test_353_repair_triage import build, cmd, report
from tests.unit.test_353_repair_triage_journal import failed_first_generation


def triage_of(manifest, *, failed, not_run=()):
    return triage_failure(manifest, report(manifest, failed, not_run=list(not_run)))


def plan_with(closure, *, touched=("c01-",), digest="d" * 64):
    triage = RepairTriage(manifest_digest="e" * 64, roots={closure[0]: "command"}, consequences=(), closure_ids=tuple(closure),
                          criterion_ids=(), requirement_ids=(), touched_parts=touched, digest=digest)
    return DeltaPlan(load_gen0()[0], triage)


# 1. split groups ------------------------------------------------------------------------------------------------------

def test_a_split_remainder_is_touched_when_its_check_is_in_the_closure():
    plan = plan_with(["c01a-foo"], touched=("c01-",))

    assert plan.group_touched(CheckGroup("c01a-", ())) is True
    assert plan.group_touched(CheckGroup("c01-", ())) is True
    assert plan.group_touched(CheckGroup("c02-", ())) is False


def test_a_sibling_remainder_is_not_touched_by_another_remainders_check():
    plan = plan_with(["c01b-foo"], touched=("c01-",))

    assert plan.group_touched(CheckGroup("c01a-", ())) is False
    assert plan.group_touched(CheckGroup("c01b-", ())) is True


@pytest.mark.asyncio
async def test_a_delta_that_changes_nothing_is_a_delta_failure_and_never_proceeds(monkeypatch):
    manifest = load_gen0()[0]
    plan = DeltaPlan(manifest, triage_of(manifest, failed=["install-helm-gh"]))

    async def unchanged(_attempt, _validate):
        return plan.base_proposal()

    monkeypatch.setattr(discovery_module, "_delta_candidate", unchanged)
    attempt = SimpleNamespace(delta=plan, budget=AttemptBudget())

    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await discovery_module._author_candidate(attempt, None, lambda data: data)
    assert caught.value.code == "repair_delta_rejected"


# 2. closure and dependency standing ----------------------------------------------------------------------------------

def test_a_dependent_of_a_root_joins_the_closure_but_a_dependent_of_a_prerequisite_does_not():
    manifest = build(install=[cmd("inst-r", ["make", "r"], depends_on=["inst-x"]), cmd("inst-x", ["make", "x"]),
                              cmd("inst-d", ["make", "d"], depends_on=["inst-r"]),
                              cmd("inst-e", ["make", "e"], depends_on=["inst-x"])])
    triage = triage_of(manifest, failed=["inst-r"])

    assert {"inst-r", "inst-x", "inst-d"} <= set(triage.closure_ids)
    assert "inst-e" not in triage.closure_ids


def chain(*, pre_env=None, pre_argv=("pre",)):
    """A check chain: c00-pre <- c01-mid <- c02-b. Only the prerequisite's content varies."""
    return build(checks=[cmd("c00-pre", list(pre_argv), **({"env": pre_env} if pre_env else {})),
                         cmd("c01-mid", ["mid"], depends_on=["c00-pre"]), cmd("c02-b", ["check", "b"], depends_on=["c01-mid"])])


def test_a_dependency_change_changes_the_standing_digest_of_every_part_that_depends_on_it():
    assert dependency_digest(chain(), ["c02-b"]) != dependency_digest(chain(pre_env={"MODE": "2"}), ["c02-b"])
    assert dependency_digest(chain(), ["c02-b"]) == dependency_digest(chain(), ["c02-b"])


def test_a_transitive_dependency_is_part_of_the_digest():
    assert dependency_digest(chain(), ["c02-b"]) != dependency_digest(chain(pre_argv=("pre", "--strict")), ["c02-b"])
    assert dependency_digest(chain(), ["c01-mid"]) != dependency_digest(chain(pre_env={"MODE": "2"}), ["c01-mid"])


# 3. duplicate content: remaps, lifecycle commands, guidance ---------------------------------------------------------

def dup_chain(*, kept_depends, kept_timeout, kept_required=True):
    previous = build(checks=[cmd("check-pre", ["pre"]), cmd("c00-old", ["quality"], depends_on=["check-pre"], timeout_seconds=60),
                             cmd("check-other", ["other"])],
                     criterion_checks={"S1": ["c00-old"]})
    candidate = build(checks=[cmd("check-pre", ["pre"]), cmd("check-x", ["x"]),
                              cmd("c00-new", ["quality"], depends_on=kept_depends, timeout_seconds=kept_timeout,
                                  required=kept_required),
                              cmd("check-other", ["other"])],
                      criterion_checks={"S1": ["c00-new"]})
    return previous, candidate


def test_a_required_duplicate_may_be_dropped_when_the_kept_copy_takes_the_union_and_the_larger_timeout():
    previous, candidate = dup_chain(kept_depends=["check-pre", "check-x"], kept_timeout=120)

    assert duplicate_aliases(previous, candidate) == {"c00-old": "c00-new"}
    preserve_obligations(candidate, previous)


def test_dropping_a_required_duplicate_without_the_union_or_the_timeout_is_refused():
    previous, candidate = dup_chain(kept_depends=["check-x"], kept_timeout=60)

    assert duplicate_aliases(previous, candidate) == {}
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        preserve_obligations(candidate, previous)
    assert caught.value.code == "obligation_removed"


def test_a_guard_does_not_freeze_a_record_that_only_a_kept_duplicate_replaces():
    previous = build(install=[cmd("install-a", ["make", "a"])],
                     checks=[cmd("c00-old", ["quality"], timeout_seconds=60), cmd("check-other", ["other"])],
                     criterion_checks={"S1": ["c00-old"]})
    candidate = build(install=[cmd("install-a", ["make", "a"])],
                      checks=[cmd("c00-new", ["quality"], timeout_seconds=60), cmd("check-other", ["other"])],
                      criterion_checks={"S1": ["c00-new"]})
    guard_repair_diff(candidate, previous, triage_of(previous, failed=["check-other"]))


def test_identical_service_lifecycle_commands_across_services_are_one_obligation_written_twice():
    services = [env.Service.model_validate({"id": "db", "start": cmd("db-start", ["db", "start"]),
                                             "health": cmd("db-health", ["db", "ping"]), "stop": cmd("db-stop", ["db", "stop"])}),
                env.Service.model_validate({"id": "cache", "start": cmd("cache-start", ["db", "start"]),
                                             "health": cmd("cache-health", ["cache", "ping"]),
                                             "stop": cmd("cache-stop", ["cache", "stop"])})]
    with pytest.raises(PydanticCustomError) as caught:
        env.validate_records([], [env.Command.model_validate(cmd("check-a", ["a"]))], services, [], {}, runtime=False)
    assert caught.value.type == "duplicate_command_content"
    assert caught.value.context == {"pairs": [["db-start", "cache-start"]]}


def test_the_duplicate_guidance_names_the_merge_the_correction_must_make():
    from coordinare.services.environment_admission import GRAPH_GUIDANCE

    guidance = GRAPH_GUIDANCE["duplicate_command_content"]
    for phrase in ("union", "timeout", "required", "criteria"):
        assert phrase in guidance


# 4. probes on fallback --------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_fallback_reselects_probe_observations_without_cache_only_reuse(monkeypatch):
    calls: list[dict] = []
    budget = AttemptBudget()
    budget.repair_triage = triage_of(load_gen0()[0], failed=["install-helm-gh"])

    attempts: list[int] = []

    async def attempt(**kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise EnvironmentDiscoveryError("delta rejected", code="repair_delta_rejected")
        return "manifest", "assessment"

    def select(reports, *_args, **kwargs):
        calls.append({"select": kwargs.get("reuse_unreferenced")})
        return reports

    monkeypatch.setattr(discovery_module, "_discover_attempt", attempt)
    monkeypatch.setattr(discovery_module, "_delta_repair", lambda *_args: True)
    monkeypatch.setattr(discovery_module, "select_current_probe_reports", select)
    await discovery_module.discover_and_review(symphony_name="suite", commit="c" * 40, paths=[], fetch=None,
        chat=_chat, budget=budget, capabilities=_capabilities(), previous_manifest=object(), execution_feedback={"results": []})

    selects = [call["select"] for call in calls if "select" in call]
    assert selects == [True, False]


async def _chat(_messages):
    return "{}"


def _capabilities():
    from tests.unit.test_351_environment_discovery import CAPABILITIES

    return CAPABILITIES


# 5. delta_fallback resets per generation -------------------------------------------------------------------------

def test_a_new_repair_generation_tries_delta_again(tmp_path):
    from coordinare.services.environment_repair import _publish

    cache = failed_first_generation(tmp_path)
    journal = journal_for(cache)
    journal.delta_fallback = True
    _publish(cache, journal)

    prepare_repair(cache)

    assert journal_for(cache).delta_fallback is False


# 6. impossible triage holds nothing ----------------------------------------------------------------------------

def test_a_rejected_execution_without_a_failed_result_prepares_a_full_repair(tmp_path):
    cache = failed_first_generation(tmp_path)
    for result in cache.environment_attempt["rejected"][-1]["report"]["results"]:
        result["status"] = "pass"

    prepare_repair(cache)

    journal = journal_for(cache)
    assert journal.repair_triage is None and journal.repair_triage_unavailable is True
    assert repair_triage_for(journal) is None


# 7. scope covers every top-level record ------------------------------------------------------------------------

def test_top_level_records_that_change_are_named_in_the_repair_scope():
    base = load_gen0()[0]
    data = base.model_dump(mode="json")
    data["activation"] = {**data["activation"], "env": {**data["activation"]["env"], "EXTRA": "1"}}
    data["empty_reason"] = "changed"
    data["runtime_endpoints"] = [{"name": "extra"}]
    data["local_credentials"] = [{"name": "TOKEN", "purpose": "fixture", "service": "db"}]

    changes = changed_ids(base, data)

    assert {"activation", "empty_reason", "runtime_endpoints", "local_credentials"} <= set(changes["changed"])


# 8. criterion rulings ------------------------------------------------------------------------------------------

def _review_data(rulings):
    return {"assessment": {"verdict": "accept", "rationale": "judged", "sources": ["a.md"], "findings": [], "decisions": [],
                           "capability_unavailable": False, "criterion_rulings": rulings}}


def _inspection():
    return SimpleNamespace(inputs={"a.md": "a" * 64}, budget=None)


def test_a_repair_review_must_rule_on_exactly_its_touched_criteria():
    ruling = {"criterion_id": "S1", "verdict": "accept", "rationale": "covered"}
    with pytest.raises(Exception) as caught:
        validate_review_part(_review_data([]), inspection=_inspection(), owned=[], capacity=8, known=ReviewIds(rulings=["S1"]))
    errors = getattr(caught.value, "errors", None)
    assert errors and errors[0]["code"] == "review_rulings_invalid" and errors[0]["required_criterion_ids"] == ["S1"]

    with pytest.raises(Exception) as extra:
        validate_review_part(_review_data([ruling, {**ruling, "criterion_id": "S9"}]), inspection=_inspection(), owned=[],
                             capacity=8, known=ReviewIds(rulings=["S1"]))
    assert extra.value.errors[0]["code"] == "review_rulings_invalid"

    proposal, _inputs = validate_review_part(_review_data([ruling]), inspection=_inspection(), owned=[], capacity=8,
                                             known=ReviewIds(rulings=["S1"]))
    assert [item.criterion_id for item in proposal.criterion_rulings] == ["S1"]


def test_rulings_are_not_required_without_a_repair_scope():
    proposal, _inputs = validate_review_part(_review_data([]), inspection=_inspection(), owned=[], capacity=8)
    assert proposal.criterion_rulings == []


def test_one_services_own_lifecycle_phases_may_share_content():
    service = env.Service.model_validate({"id": "db", "start": cmd("db-start", ["true"]), "health": cmd("db-health", ["true"]),
                                          "stop": cmd("db-stop", ["db", "stop"])})
    env.validate_records([], [env.Command.model_validate(cmd("check-a", ["a"]))], [service], [], {}, runtime=False)


class _CountingInspection:
    def __init__(self, *_args):
        self.inputs: dict[str, str] = {}

    async def bind_verified(self, expected, **_kwargs):
        self.inputs.update(expected)
        return [{"path": path} for path in expected]


@pytest.mark.asyncio
async def test_a_part_is_judged_again_when_a_prerequisite_it_reads_changes(monkeypatch):
    from coordinare.services import environment_discovery as discovery
    from tests.unit.test_353_delta_repair import FakeCriterion

    def manifest_with(env):
        base = build(checks=[cmd("c00-pre", ["pre"], **({"env": env} if env else {})),
                             cmd("c01-mid", ["mid"], depends_on=["c00-pre"])])
        return base.model_copy(update={"criterion_checks": {"S1": ["c00-pre"], "S2": ["c01-mid"]}})

    first = manifest_with(None)
    source = sorted(first.inputs)[0]
    criteria = [FakeCriterion(id="S1", description="one", applicability="required", sources=[source], prefix="c00-"),
                FakeCriterion(id="S2", description="two", applicability="required", sources=[source], prefix="c01-")]
    groups = [CheckGroup("c00-", (criteria[0],)), CheckGroup("c01-", (criteria[1],))]
    budget = AttemptBudget(max_calls=64, max_reads=64, max_coverage_revisions=4)
    budget.finding_ledger = FindingLedger(source_commit=first.source_commit)
    calls: list[str] = []

    async def agent(_context, inspection, messages, task):
        # A group part is named by its criterion; the global part has no criterion mapping in its view.
        own = json.loads(messages[1]["content"])["own"]
        calls.append(",".join(sorted(own["part"].get("criterion_checks", {}))) or "global")
        from tests.unit.test_353_delta_repair import assessment
        reply = {"assessment": {**assessment("accept", sorted(inspection.inputs)).model_dump(mode="json"), "criterion_rulings": []}}
        return task.validate(reply)

    monkeypatch.setattr(discovery, "_Inspection", _CountingInspection)
    monkeypatch.setattr(discovery, "run_agent", agent)
    monkeypatch.setattr(discovery, "manifest_obligation_ids", lambda _manifest: set())
    build_stub = SimpleNamespace(groups=groups, scope=SimpleNamespace(definition=SimpleNamespace(criteria=criteria)),
                                 context=None, concurrency=4)
    attempt = SimpleNamespace(budget=budget, delta=None, identity={"symphony_name": "suite"},
                              discovery=SimpleNamespace(finding_responses=[]))
    reviewer = SimpleNamespace(entries=None, paths=[], fetch=None, limits=None, inputs={})

    await discovery._divided_review(build_stub, attempt, first, reviewer)
    assert sorted(calls) == ["S1", "S2", "global"]  # each part judged once

    calls.clear()
    await discovery._divided_review(build_stub, attempt, first, reviewer)
    assert calls == []  # unchanged content keeps its standing

    calls.clear()
    changed = manifest_with({"MODE": "2"})  # only the prerequisite of c01's check changes
    await discovery._divided_review(build_stub, attempt, changed, reviewer)
    # c00 changed its own check; c01 is re-judged through its dependency digest although its own check is unchanged.
    assert sorted(calls) == ["S1", "S2"]


def test_a_dropped_check_is_aliased_to_the_identical_check_not_to_an_identical_install_command():
    """Review r4 of the pinned-copy fold: only a check can keep a dropped check's criterion coverage."""
    previous = build(install=[cmd("install-tool", ["tool", "--version"])],
                     checks=[cmd("c00-tool", ["tool", "--version"]), cmd("check-other", ["other"])],
                     criterion_checks={"S1": ["c00-tool"]})
    candidate = build(install=[cmd("install-tool", ["tool", "--version"])],
                      checks=[cmd("env-tool", ["tool", "--version"]), cmd("check-other", ["other"])],
                      criterion_checks={"S1": ["env-tool"]})
    assert duplicate_aliases(previous, candidate) == {"c00-tool": "env-tool"}
    preserve_obligations(candidate, previous)


def test_a_bare_indeterminate_review_part_is_sent_back():
    """Dettmore e2e-36: c01's reviewer returned indeterminate with no finding, ruling or capability gap."""
    from coordinare.services.environment_findings import BARE_INDETERMINATE

    data = _review_data([])
    data["assessment"]["verdict"] = "indeterminate"
    with pytest.raises(Exception) as caught:
        validate_review_part(data, inspection=_inspection(), owned=[], capacity=8)
    assert caught.value.errors[0]["code"] == "review_findings_invalid"
    assert caught.value.errors[0]["guidance"] == BARE_INDETERMINATE
    data["assessment"]["findings"] = [{"category": "capability", "issue": "Database engine evidence is missing",
                                       "sources": ["a.md"], "affected_ids": []}]
    proposal, _inputs = validate_review_part(data, inspection=_inspection(), owned=[], capacity=8)
    assert proposal.verdict == "indeterminate"
