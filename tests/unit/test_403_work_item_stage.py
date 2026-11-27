"""403: the stage contract for a stage whose input is a work item, not a pull request."""
from __future__ import annotations

import re
from typing import Any

import pytest

from coordinare.services.stage_contract import (
    ExternalStageService,
    JobState,
    PullRequestSnapshot,
    StageJob,
    StageOutcome,
    StageVerdict,
    WorkItemRequest,
    WorkItemSnapshot,
    WorkItemStageService,
    parse_issue_url,
    work_item_digest,
    work_item_pin,
)
from coordinare.transport.base import TransportError
from coordinare.transport.http_transport import PerformerAuthError

TITLE = "Export widgets as CSV"
BODY = "## Acceptance\n- [ ] a CSV download exists\n"
CARD: dict[str, Any] = {
    "id": "PVTI_card1",
    "issue_url": "https://github.com/acme/widgets/issues/12",
    "issue_id": "I_node12",
    "issue_number": 12,
    "title": TITLE,
}
BLUEPRINT = {"summary": TITLE, "milestones": [{"goal": "CSV", "scope": ["export.py"], "done_when": "done"}]}


class FakeIssues:
    def __init__(self) -> None:
        self.title, self.body, self.fail = TITLE, BODY, False
        self.reads: list[tuple[str, str]] = []

    async def read(self, repository: str, node: str) -> WorkItemSnapshot:
        self.reads.append((repository, node))
        if self.fail:
            msg = "GitHub is down"
            raise ConnectionError(msg)
        return WorkItemSnapshot(title=self.title, body=self.body, state="OPEN")


class FakePlanner:
    """A provider whose jobs the test sets by hand."""

    name = "planner"

    def __init__(self) -> None:
        self.jobs: dict[str, StageJob] = {}
        self.submitted: list[tuple[str, WorkItemRequest]] = []

    async def submit(self, request: Any, request_id: str) -> StageJob:
        self.submitted.append((request_id, request))
        return self.jobs.setdefault(request_id, StageJob(ref=request_id, state=JobState.PENDING, detail="running"))

    async def poll(self, ref: str) -> StageJob:
        return self.jobs[ref]

    async def health(self) -> tuple[bool, str]:
        return True, "ok"

    def finish(self, ref: str, outcome: StageOutcome) -> None:
        self.jobs[ref] = StageJob(ref=ref, state=JobState.DONE, outcome=outcome)


def _service(planner: FakePlanner, issues: FakeIssues) -> WorkItemStageService:
    return WorkItemStageService(planner, stage="architecting", work_item_reader=issues.read)


def _ref(session_id: str) -> str:
    return session_id.split("@", 1)[0]


def _pin(*, title: str = TITLE, body: str = BODY, clarifications: Any = None) -> str:
    return work_item_pin(work_item_digest(title, body), clarifications or [])


def _passed(pin: str, *, title: str = TITLE, body: str = BODY) -> StageOutcome:
    return StageOutcome(
        verdict=StageVerdict.PASSED, reviewed_head=pin, input_digest=work_item_digest(title, body),
        blueprint=dict(BLUEPRINT), record_ref="/runs/x",
    )


@pytest.fixture
def planner() -> FakePlanner:
    return FakePlanner()


@pytest.fixture
def issues() -> FakeIssues:
    return FakeIssues()


# -- the request -------------------------------------------------------------------------


def _request(**kw: Any) -> WorkItemRequest:
    base: dict[str, Any] = {
        "card_id": "PVTI_card1", "stage": "architecting", "repository": "acme/widgets",
        "number": 12, "node_id": "I_node12", "pin": _pin(),
    }
    base.update(kw)
    return WorkItemRequest(**base)


def test_request_id_names_card_stage_pin_and_attempt() -> None:
    assert _request().request_id(1) == _request().request_id(1)
    ids = {
        _request().request_id(1),
        _request().request_id(2),
        _request(pin=_pin(body=BODY + "- [ ] and JSON\n")).request_id(1),
        _request(card_id="PVTI_other").request_id(1),
    }
    assert len(ids) == 4
    assert re.fullmatch(r"[A-Za-z0-9.-]{1,128}", _request(card_id="x" * 500).request_id(20))


def test_the_pin_moves_with_the_text_and_the_answers_only() -> None:
    assert _pin() == _pin()
    assert _pin(title="Other") != _pin()
    assert _pin(body=BODY + "more") != _pin()
    assert _pin(clarifications=[{"questions": ["Which format?"], "answer": "CSV"}]) != _pin()
    # Line endings are GitHub's business, not a change of requirements.
    assert _pin(body=BODY.replace("\n", "\r\n")) == _pin()


def test_parse_issue_url() -> None:
    assert parse_issue_url("https://github.com/acme/widgets/issues/12") == ("github.com", "acme/widgets", 12)
    assert parse_issue_url("https://ghe.example/acme/widgets/issues/3/") == ("ghe.example", "acme/widgets", 3)
    assert parse_issue_url("https://github.com/acme/widgets/pull/12") is None
    assert parse_issue_url("https://acme.atlassian.net/browse/WID-12") is None
    assert parse_issue_url("") is None


def test_a_pull_request_service_refuses_a_work_item_stage(planner: FakePlanner) -> None:
    async def _no_pr(repository: str, node: str) -> PullRequestSnapshot:
        return PullRequestSnapshot(head_sha="a" * 40)

    with pytest.raises(ValueError, match="work item"):
        ExternalStageService(planner, stage="architecting", pr_reader=_no_pr)


# -- dispatch ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_submits_the_issue_pinned_to_what_the_planner_reads(
    planner: FakePlanner, issues: FakeIssues,
) -> None:
    accepted = await _service(planner, issues).dispatch_card(dict(CARD))
    assert accepted["status"] == "accepted"
    assert accepted["performer_id"] == "stage-provider:planner:architecting"
    ((request_id, request),) = planner.submitted
    assert request.repository == "acme/widgets"
    assert request.number == 12
    assert request.pin == _pin()
    assert accepted["session_id"] == f"{request_id}@acme/widgets#12@I_node12"
    assert issues.reads == [("acme/widgets", "I_node12")]


@pytest.mark.asyncio
async def test_an_answer_is_a_new_request(planner: FakePlanner, issues: FakeIssues) -> None:
    service = _service(planner, issues)
    await service.dispatch_card(dict(CARD))
    answered = {**CARD, "clarifications": [{"questions": ["Which format?"], "answer": "CSV"}]}
    await service.dispatch_card(answered)
    first, second = (rid for rid, _ in planner.submitted)
    assert first != second


@pytest.mark.parametrize("card", [
    {**CARD, "issue_url": "", "issue_id": ""},
    {**CARD, "issue_url": "https://acme.atlassian.net/browse/WID-12", "issue_id": "WID-12"},
    {**CARD, "issue_id": ""},
])
@pytest.mark.asyncio
async def test_a_work_item_that_is_not_a_github_issue_blocks_and_names_progettare_68(
    planner: FakePlanner, issues: FakeIssues, card: dict[str, Any],
) -> None:
    with pytest.raises(PerformerAuthError, match=r"ViviDynamics/progettare#68"):
        await _service(planner, issues).dispatch_card(card)
    assert planner.submitted == []


@pytest.mark.asyncio
async def test_an_unreadable_issue_is_a_retryable_error(planner: FakePlanner, issues: FakeIssues) -> None:
    issues.fail = True
    with pytest.raises(TransportError, match="could not read the work item"):
        await _service(planner, issues).dispatch_card(dict(CARD))
    assert planner.submitted == []


# -- outcomes ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_plan_passes_the_stage_with_its_blueprint(planner: FakePlanner, issues: FakeIssues) -> None:
    service = _service(planner, issues)
    session = (await service.dispatch_card(dict(CARD)))["session_id"]
    assert (await service.check_status(session))["status"] == "working"
    planner.finish(_ref(session), _passed(_pin()))
    status = await service.check_status(session)
    assert status["status"] == "plan_committed"
    assert status["report"]["blueprint"] == BLUEPRINT
    assert status["report"]["stage_record"]["record_ref"] == "/runs/x"


@pytest.mark.asyncio
async def test_a_restarted_daemon_polls_and_redispatches_the_same_run(
    planner: FakePlanner, issues: FakeIssues,
) -> None:
    session = (await _service(planner, issues).dispatch_card(dict(CARD)))["session_id"]
    restarted = _service(planner, issues)
    assert (await restarted.check_status(session))["status"] == "working"
    again = await restarted.dispatch_card(dict(CARD))
    assert again["session_id"] == session
    planner.finish(_ref(session), _passed(_pin()))
    # A finished plan is found again, never planned a second time.
    assert (await restarted.dispatch_card(dict(CARD)))["session_id"] == session
    assert {rid for rid, _ in planner.submitted} == {_ref(session)}


@pytest.mark.asyncio
async def test_a_plan_of_text_that_changed_since_is_refused(planner: FakePlanner, issues: FakeIssues) -> None:
    service = _service(planner, issues)
    session = (await service.dispatch_card(dict(CARD)))["session_id"]
    planner.finish(_ref(session), _passed(_pin()))
    issues.body = BODY + "- [ ] and JSON\n"
    with pytest.raises(TransportError, match="refused"):
        await service.check_status(session)
    # The next dispatch plans the current text under a new request.
    await service.dispatch_card(dict(CARD))
    assert planner.submitted[-1][0] != _ref(session)


@pytest.mark.asyncio
async def test_blocked_shows_the_planners_questions(planner: FakePlanner, issues: FakeIssues) -> None:
    service = _service(planner, issues)
    session = (await service.dispatch_card(dict(CARD)))["session_id"]
    planner.finish(_ref(session), StageOutcome(
        verdict=StageVerdict.NEEDS_HUMAN, reviewed_head=_pin(), input_digest=work_item_digest(TITLE, BODY),
        summary="blocked at intake", questions=["What are the acceptance criteria?"],
    ))
    status = await service.check_status(session)
    assert status["status"] == "blocked"
    assert status["questions"] == ["What are the acceptance criteria?"]
    assert "blocked at intake" in status["reason"]


@pytest.mark.asyncio
async def test_a_failed_run_is_retried_under_the_next_attempt(planner: FakePlanner, issues: FakeIssues) -> None:
    service = _service(planner, issues)
    session = (await service.dispatch_card(dict(CARD)))["session_id"]
    planner.jobs[_ref(session)] = StageJob(ref=_ref(session), state=JobState.RETRYABLE, reason="failed at survey")
    with pytest.raises(TransportError, match="failed at survey"):
        await service.check_status(session)
    retry = await service.dispatch_card(dict(CARD))
    assert _ref(retry["session_id"]) == _request().request_id(2)


@pytest.mark.asyncio
async def test_an_operator_restart_plans_again(planner: FakePlanner, issues: FakeIssues) -> None:
    service = _service(planner, issues)
    session = (await service.dispatch_card(dict(CARD)))["session_id"]
    planner.finish(_ref(session), _passed(_pin()))
    service.set_fresh_attempt("PVTI_card1", allowed=True)
    fresh = await service.dispatch_card(dict(CARD))
    assert _ref(fresh["session_id"]) == _request().request_id(2)
