"""nare-native-028: a delta review rejected fell straight back to the full repair, and the repair's timing counted the
delta and the full repair as one, so the time gate expected the next delta to cost both. A review-rejected delta now
gets one revision round first; the timing keeps the delta and the full repair apart, the next delta is estimated from
its delta part, and the fallback is gated on its own measured seconds."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_discovery import (
    DiscoveryLimits,
    _delta_repair,
    _fallback_time_gate,
)
from coordinare.services.environment_repair import (
    _generation_timing,
    fallback_time_estimate,
    repair_time_estimate,
)
from coordinare.services.environment_shard_construction import delta_part_key
from tests.unit.test_353_delta_repair import delta_run
from tests.unit.test_353_discovery_repair import assessment, candidate, finding_responses


def _delta_chat(reviews: list[str], kinds: list[str], revisions: list[dict[str, Any]]) -> Any:
    """Reviewers answer the verdicts in order; authors are told apart by their brief and the revision they were sent."""
    async def chat(messages: list[dict[str, str]]) -> str:
        context = json.loads(messages[1]["content"])
        if "manifest" in context:
            verdict = reviews.pop(0)
            return json.dumps(assessment(candidate(), verdict, retained=bool(kinds) and len(kinds) > 1))
        last = json.loads(messages[-1]["content"])
        if "repair_delta" in context:
            kinds.append("delta")
            if "delta_revision" in last:
                revisions.append(last)
            mapping = {criterion["id"]: ["version"] for criterion in context["scope_contract"]["definition"]["criteria"]}
            responses = finding_responses() if "delta_revision" in last else []
            return json.dumps({"manifest_delta": {"criterion_checks": mapping, "checks": [{"id": "version", "argv": ["zog", "--version"], "required": True, "timeout_seconds": 600}]},
                               "finding_responses": responses})
        kinds.append("full")
        return json.dumps({"manifest": candidate(), "finding_responses": finding_responses()})
    return chat


@pytest.mark.asyncio
async def test_a_review_rejected_delta_is_revised_once_by_its_author_and_needs_no_fallback() -> None:
    kinds: list[str] = []
    revisions: list[dict[str, Any]] = []
    budget = AttemptBudget()
    await delta_run(_delta_chat(["revise", "accept"], kinds, revisions), budget=budget)

    assert kinds == ["delta", "delta"]
    assert budget.delta_revisions == 1 and budget.coverage_revisions == 1
    assert budget.delta_fallback is False and budget.repair_delta is True
    [revision] = revisions
    assert revision["delta_revision"]["finding_ledger"]["records"]
    assert "Return manifest_delta again" in revision["instruction"]


@pytest.mark.asyncio
async def test_a_delta_review_rejects_again_falls_back_to_the_full_repair() -> None:
    kinds: list[str] = []
    budget = AttemptBudget()
    await delta_run(_delta_chat(["revise", "revise", "accept"], kinds, []), budget=budget)

    assert kinds == ["delta", "delta", "full"]
    assert budget.delta_fallback is True and budget.delta_fallback_code == "repair_delta_rejected"
    assert budget.delta_fallback_at is not None


@pytest.mark.asyncio
async def test_without_a_coverage_revision_left_a_rejected_delta_falls_back_at_once() -> None:
    kinds: list[str] = []
    budget = AttemptBudget(max_coverage_revisions=0)
    with pytest.raises(EnvironmentDiscoveryError):
        await delta_run(_delta_chat(["revise", "revise"], kinds, []), budget=budget)
    assert kinds[:2] == ["delta", "full"]
    assert budget.delta_revisions == 0


def test_a_revision_round_keys_its_parts_apart_and_round_zero_keeps_its_key() -> None:
    digest = "d" * 64
    assert delta_part_key(digest, None, 0) == delta_part_key(digest, None)
    assert delta_part_key(digest, None, 1) == f"{delta_part_key(digest, None)}-r1"
    policy = {"no_new_privileges": True}
    assert delta_part_key(digest, policy, 1) != delta_part_key(digest, policy) == delta_part_key(digest, policy, 0)


def test_a_restart_inside_a_delta_revision_takes_the_full_repair() -> None:
    budget = AttemptBudget()
    budget.repair_triage = object()  # type: ignore[assignment]
    options = SimpleNamespace(previous_manifest=object(), execution_feedback={"results": []})
    assert _delta_repair(options, DiscoveryLimits(), budget)
    budget.delta_revisions = 1
    assert not _delta_repair(options, DiscoveryLimits(), budget)


def _journal(**fields: Any) -> Any:
    values = {"generation": 1, "generation_started_at": 1000.0, "executing_started_at": 4000.0, "started_at": 0.0,
              "generation_install_only": False, "fallback_started_at": None, "expires_at": 28800.0, "rejected": []}
    return SimpleNamespace(**{**values, **fields})


def test_a_fallen_back_repair_records_its_delta_and_full_repair_apart() -> None:
    timing = _generation_timing(_journal(fallback_started_at=2000.0), 4800.0)
    assert timing is not None
    assert (timing["discovery_seconds"], timing["delta_seconds"], timing["fallback_seconds"]) == (3000.0, 1000.0, 2000.0)
    plain = _generation_timing(_journal(), 4800.0)
    assert plain is not None and "delta_seconds" not in plain


def test_the_next_delta_is_estimated_from_the_delta_and_a_full_repair_from_the_fallback() -> None:
    first = {"timing": {"generation": 0, "discovery_seconds": 9000.0, "execution_seconds": 700.0}}
    fell_back = {"timing": {"generation": 1, "install_only": False, "discovery_seconds": 5107.0, "delta_seconds": 2040.0,
                            "fallback_seconds": 3067.0, "execution_seconds": 783.0}}
    rejected = [first, fell_back]
    assert repair_time_estimate(rejected, install_only=False, delta=True)["discovery_seconds"] == 2040.0
    assert repair_time_estimate(rejected, install_only=False, delta=False)["discovery_seconds"] == 3067.0
    # A repair that never fell back counts whole either way.
    assert repair_time_estimate([first], install_only=False, delta=True)["discovery_seconds"] == 1800.0
    assert fallback_time_estimate(_journal(rejected=rejected)) == 3067.0 + 783.0 + 600.0
    assert fallback_time_estimate(_journal(rejected=[first])) is None


def test_the_fallback_is_held_when_its_measured_seconds_do_not_fit() -> None:
    budget = AttemptBudget(timeout_seconds=3600)
    budget.fallback_estimate_seconds = 7200.0
    with pytest.raises(EnvironmentDiscoveryError) as raised:
        _fallback_time_gate(budget)
    assert raised.value.code == "attempt_time_insufficient"
    budget.fallback_estimate_seconds = 60.0
    _fallback_time_gate(budget)
    budget.fallback_estimate_seconds = None
    _fallback_time_gate(budget)


def test_the_fallback_time_and_revision_count_survive_a_restart(tmp_path: Any) -> None:
    from coordinare.services.environment_repair import (
        begin_attempt,
        journal_for,
        prepare_repair,
        save_attempt,
    )
    from tests.unit.test_353_repair_triage_journal import failed_first_generation

    cache = failed_first_generation(tmp_path)
    prepare_repair(cache)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=120)
    assert budget.delta_fallback_at is None and budget.delta_revisions == 0
    budget.delta_fallback_at, budget.delta_revisions = time.time(), 1
    save_attempt(cache, budget)
    restarted = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=121)
    assert restarted.delta_fallback_at == budget.delta_fallback_at and restarted.delta_revisions == 1
    journal = journal_for(cache)
    assert journal.fallback_started_at == budget.delta_fallback_at and journal.delta_revisions == 1


def test_a_divided_delta_revision_routes_findings_to_its_check_groups_and_shows_the_global_author_its_delta() -> None:
    from coordinare.services import environment_discovery as discovery_module

    routed: list[Any] = []
    build = SimpleNamespace(route_findings=routed.append)
    from coordinare_service_inference.environment.findings import FindingLedger

    ledger = FindingLedger(source_commit="a" * 40)
    attempt = SimpleNamespace(messages=[{"role": "system", "content": ""}, {"role": "user", "content": "{}"}],
                              delta_reply={"manifest_delta": {"install": []}, "finding_responses": []})
    review = SimpleNamespace(finding_ledger=ledger, rationale="needs a sample", sources=["README.md"])
    discovery_module._delta_revision(attempt, build, review)  # type: ignore[arg-type]

    assert routed == [ledger]
    assistant, user = attempt.messages[2:]
    assert json.loads(assistant["content"]) == attempt.delta_reply
    assert json.loads(user["content"])["delta_revision"]["untrusted_review_feedback"] == "needs a sample"


def test_a_delta_author_answering_an_unknown_finding_is_corrected_in_its_own_phase() -> None:
    """nare-native-029: the global delta author answered F0007, which no review raised; only the merge checked the
    ids, and a merge error discards a delta, so the attempt fell back. The author's own phase now refuses it with the
    existing correction (finding_response_ids_invalid lists the allowed ids)."""
    from coordinare_service_inference.environment.findings import FindingLedger, FindingResponse

    from coordinare.services import environment_discovery as discovery_module
    from coordinare.services.environment_findings import FindingValidationError

    budget = AttemptBudget()
    budget.finding_ledger = FindingLedger(source_commit="a" * 40)
    response = {"finding_id": "F0007", "rationale": "r", "sources": ["README.md"], "affected_ids": []}
    discovery = SimpleNamespace(finding_responses=[FindingResponse.model_validate(response)])
    with pytest.raises(FindingValidationError) as raised:
        discovery_module._delta_response_ids(discovery, budget)  # type: ignore[arg-type]
    assert raised.value.code == "finding_response_ids_invalid"
    discovery.finding_responses = []
    discovery_module._delta_response_ids(discovery, budget)  # type: ignore[arg-type]
