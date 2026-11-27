"""404: the stage contract applied to QA: inputs, the evidence floor, and the replies."""
from __future__ import annotations

from typing import Any

import pytest

from coordinare.services.qa_verdict import classify_qa_verdict
from coordinare.services.stage_contract import (
    CriterionOutcome,
    ExternalStageService,
    JobState,
    PullRequestSnapshot,
    StageJob,
    StageOutcome,
    StageRequest,
    StageVerdict,
    criteria_from_card,
)
from coordinare.transport.base import TransportError

HEAD = "a" * 40
BASE = "b" * 40
NEXT = "c" * 40
CARD = {
    "id": "PVTI_card1",
    "pr_url": "https://github.com/acme/widgets/pull/7",
    "pr_node_id": "PR_node7",
    "issue_number": 5,
    "issue_id": "I_node5",
    "acceptance_criteria": ["The notice shows", "The test passes"],
}


class FakeQA:
    """A QA provider that finishes each job with whatever outcome the test sets."""

    name = "qare"

    def __init__(self) -> None:
        self.requests: list[tuple[StageRequest, str]] = []
        self.outcome: StageOutcome | None = None
        self.published: list[tuple[str, StageOutcome]] = []
        self.publish_error: Exception | None = None

    async def submit(self, request: StageRequest, request_id: str) -> StageJob:
        self.requests.append((request, request_id))
        return StageJob(ref=request_id, state=JobState.PENDING)

    async def poll(self, ref: str) -> StageJob:
        if self.outcome is None:
            return StageJob(ref=ref, state=JobState.PENDING)
        return StageJob(ref=ref, state=JobState.DONE, outcome=self.outcome)

    async def health(self) -> tuple[bool, str]:
        return True, "ok"

    async def publish(self, ref: str, outcome: StageOutcome) -> StageOutcome:
        if self.publish_error is not None:
            raise self.publish_error
        self.published.append((ref, outcome))
        return outcome.model_copy(update={"record_ref": "https://github.com/acme/widgets/pull/7#issuecomment-1"})


class FakeGitHub:
    def __init__(self) -> None:
        self.head = HEAD

    async def read(self, repository: str, node: str) -> PullRequestSnapshot:
        return PullRequestSnapshot(head_sha=self.head, base_sha=BASE, base_ref="main")


def _outcome(verdict: StageVerdict, outcomes: list[str], head: str = HEAD) -> StageOutcome:
    return StageOutcome(
        verdict=verdict,
        reviewed_head=head,
        summary="summary",
        provider_verdict={StageVerdict.PASSED: "passed", StageVerdict.CHANGES_REQUESTED: "failed"}.get(verdict, "blocked"),
        criteria=[
            CriterionOutcome(id=f"ac-{i}", text=f"criterion {i}", outcome=o, reason="" if o == "proven" else "nope",
                             evidence=[f"https://github.com/acme/widgets/blob/qa-assets/qare/x/{i}.txt"])
            for i, o in enumerate(outcomes)
        ],
    )


async def _run(outcome: StageOutcome, github: FakeGitHub | None = None) -> tuple[FakeQA, dict[str, Any]]:
    provider = FakeQA()
    github = github or FakeGitHub()
    service = ExternalStageService(provider, stage="qa", pr_reader=github.read)
    accepted = await service.dispatch_card(dict(CARD))
    provider.outcome = outcome
    return provider, await service.check_status(accepted["session_id"])


# -- inputs --------------------------------------------------------------------------------


def test_criteria_come_from_the_card_with_ids_stable_across_order() -> None:
    first = criteria_from_card({"acceptance_criteria": ["A notice", "A  test"]})
    second = criteria_from_card({"acceptance_criteria": ["A test", "A notice", "A notice"]})
    assert sorted(c.id for c in first) == sorted(c.id for c in second)
    assert len(second) == 2
    assert all(c.id.startswith("ac-") for c in first)


def test_the_verification_brief_wins_over_the_card() -> None:
    criteria = criteria_from_card({
        "acceptance_criteria": ["from the card"],
        "verification_brief": {"criteria": [{"surface": "/payouts", "action": "open it", "expected": "the notice"}]},
    })
    assert [c.text for c in criteria] == ["/payouts: open it -> the notice"]


@pytest.mark.asyncio
async def test_the_request_carries_base_issue_and_criteria() -> None:
    provider, _ = await _run(_outcome(StageVerdict.PASSED, ["proven", "proven"]))
    request, request_id = provider.requests[0]
    assert request.base_sha == BASE
    assert request.base_ref == "main"
    assert request.issue_number == 5
    assert request.issue_node_id == "I_node5"
    assert [c.text for c in request.criteria] == CARD["acceptance_criteria"]
    # The request id is still card, stage, head and attempt only.
    bare = StageRequest(card_id="PVTI_card1", stage="qa", repository="acme/widgets", pr_number=7,
                        pr_node_id="PR_node7", head_sha=HEAD)
    assert request_id == bare.request_id(1)


# -- outcomes ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_pass_with_every_criterion_proven_passes_qa() -> None:
    provider, status = await _run(_outcome(StageVerdict.PASSED, ["proven", "proven"]))
    assert status["status"] == "qa_passed"
    assert status["head_sha"] == HEAD
    report = status["report"]
    assert (report["criteria_checked"], report["criteria_passed"]) == (2, 2)
    assert report["stage_record"]["provider_verdict"] == "passed"
    assert report["stage_record"]["record_ref"].endswith("issuecomment-1")
    # The 120 evidence floor the monitor applies lets it through.
    assert classify_qa_verdict("qa_passed", report) == "advance"
    assert len(provider.published) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("outcomes", [["proven", "unverified"], ["proven", "failed"], []])
async def test_a_pass_without_every_criterion_proven_is_held(outcomes: list[str]) -> None:
    _, status = await _run(_outcome(StageVerdict.PASSED, outcomes))
    assert status["status"] == "blocked"
    assert "for a person" in status["reason"]


@pytest.mark.asyncio
async def test_a_failure_goes_to_the_implementer_with_the_failing_criteria() -> None:
    _, status = await _run(_outcome(StageVerdict.CHANGES_REQUESTED, ["proven", "failed"]))
    assert status["status"] == "qa_failed"
    assert [f["test"] for f in status["failures"]] == ["ac-1"]
    failure = status["failures"][0]
    assert failure["criterion"] == "criterion 1"
    assert failure["actual"] == "nope"
    assert failure["evidence"][0].startswith("https://github.com/acme/widgets/blob/qa-assets/")
    assert status["head_sha"] == HEAD


@pytest.mark.asyncio
async def test_blocked_refused_and_waived_hold_the_card() -> None:
    _, status = await _run(_outcome(StageVerdict.NEEDS_HUMAN, ["unverified", "proven"]))
    assert status["status"] == "blocked"
    assert "summary" in status["reason"]
    assert status["report"]["stage_record"]["criteria"][0]["outcome"] == "unverified"


@pytest.mark.asyncio
async def test_a_result_for_an_older_head_is_refused_and_not_published() -> None:
    github = FakeGitHub()
    provider = FakeQA()
    service = ExternalStageService(provider, stage="qa", pr_reader=github.read)
    accepted = await service.dispatch_card(dict(CARD))
    provider.outcome = _outcome(StageVerdict.PASSED, ["proven"])
    github.head = NEXT
    with pytest.raises(TransportError, match="result is refused"):
        await service.check_status(accepted["session_id"])
    assert provider.published == []


@pytest.mark.asyncio
async def test_a_publish_that_fails_is_retried_not_passed() -> None:
    provider = FakeQA()
    github = FakeGitHub()
    service = ExternalStageService(provider, stage="qa", pr_reader=github.read)
    accepted = await service.dispatch_card(dict(CARD))
    provider.outcome = _outcome(StageVerdict.PASSED, ["proven"])
    provider.publish_error = RuntimeError("GitHub said no")
    with pytest.raises(TransportError, match="could not publish"):
        await service.check_status(accepted["session_id"])
