"""402: the stage contract adapter, driven end to end against a fake Scrutare."""
from __future__ import annotations

import re
from typing import Any

import pytest

from coordinare.services.scrutare import ScrutareProvider
from coordinare.services.stage_contract import (
    ExternalStageService,
    JobState,
    PullRequestSnapshot,
    StageJob,
    StageRequest,
    parse_pr_url,
)
from coordinare.stage_provider_config import ScrutareProviderConfig
from coordinare.transport.base import TransportError
from tests.unit.fake_scrutare import LOGIN, TOKEN, FakeScrutare

HEAD = "a" * 40
NEXT = "c" * 40
CARD = {
    "id": "PVTI_card1",
    "pr_url": "https://github.com/acme/widgets/pull/7",
    "pr_node_id": "PR_node7",
    "title": "Widget",
}


class FakeGitHub:
    """The pull request as GitHub would show it: a head and the reviews on it."""

    def __init__(self, head: str = HEAD) -> None:
        self.head = head
        self.reviews: list[dict[str, Any]] = []
        self.fail = False
        self.reads: list[tuple[str, str]] = []

    async def read(self, repository: str, node: str) -> PullRequestSnapshot:
        self.reads.append((repository, node))
        if self.fail:
            msg = "GitHub is down"
            raise ConnectionError(msg)
        return PullRequestSnapshot(head_sha=self.head, reviews=list(self.reviews))


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeScrutare:
    monkeypatch.setenv("SCRUTARE_TEST_TOKEN", TOKEN)
    return FakeScrutare()


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


def _service(fake: FakeScrutare, github: FakeGitHub) -> ExternalStageService:
    config = ScrutareProviderConfig(
        kind="scrutare", url="http://scrutare.test", token_env="SCRUTARE_TEST_TOKEN",
        installation=169458182, review_login=LOGIN,
    )
    provider = ScrutareProvider("scrutare", config, transport=fake.transport)
    return ExternalStageService(provider, stage="reviewing", pr_reader=github.read)


def _job_of(session_id: str) -> str:
    return session_id.split("@", 1)[0]


# -- the request ------------------------------------------------------------------------


def _request(**kw: Any) -> StageRequest:
    base: dict[str, Any] = {
        "card_id": "PVTI_card1", "stage": "reviewing", "repository": "acme/widgets",
        "pr_number": 7, "pr_node_id": "PR_node7", "head_sha": HEAD,
    }
    base.update(kw)
    return StageRequest(**base)


def test_request_id_is_stable_and_names_card_stage_head_and_attempt() -> None:
    assert _request().request_id(1) == _request().request_id(1)
    ids = {
        _request().request_id(1),
        _request().request_id(2),
        _request(head_sha=NEXT).request_id(1),
        _request(card_id="PVTI_other").request_id(1),
        _request(stage="architecting").request_id(1),
    }
    assert len(ids) == 5


def test_request_id_is_one_scrutare_accepts() -> None:
    # Scrutare prefixes "api:" and admits [A-Za-z0-9_.:-]{1,128} (interfaces/app.py).
    rid = "api:" + _request(card_id="x" * 500).request_id(20)
    assert re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", rid)


def test_attempts_start_at_one() -> None:
    with pytest.raises(ValueError, match="start at 1"):
        _request().request_id(0)


def test_parse_pr_url() -> None:
    assert parse_pr_url("https://github.com/acme/widgets/pull/7") == ("acme/widgets", 7)
    assert parse_pr_url("https://ghe.example/acme/widgets/pull/12/files") == ("acme/widgets", 12)
    assert parse_pr_url("https://github.com/acme/widgets/issues/7") is None


# -- dispatch and polling -----------------------------------------------------------------


async def test_dispatch_submits_the_current_head(fake: FakeScrutare, github: FakeGitHub) -> None:
    service = _service(fake, github)
    accepted = await service.dispatch_card(dict(CARD))
    assert accepted["status"] == "accepted"
    assert accepted["performer_id"] == "stage-provider:scrutare:reviewing"
    rid = _request().request_id(1)
    assert accepted["session_id"].startswith(fake.by_request[rid] + "@acme/widgets#7@PR_node7")


async def test_a_queued_job_is_working(fake: FakeScrutare, github: FakeGitHub) -> None:
    service = _service(fake, github)
    accepted = await service.dispatch_card(dict(CARD))
    status = await service.check_status(accepted["session_id"])
    assert status["status"] == "working"


async def test_approve_passes_the_stage_for_the_reviewed_head(fake: FakeScrutare, github: FakeGitHub) -> None:
    service = _service(fake, github)
    accepted = await service.dispatch_card(dict(CARD))
    fake.post(_job_of(accepted["session_id"]), verdict="approve", head=HEAD)
    status = await service.check_status(accepted["session_id"])
    assert status["status"] == "approved"
    assert status["head_sha"] == HEAD
    record = status["report"]["stage_record"]
    assert record["verdict"] == "pass"
    assert record["reviewed_head"] == HEAD
    assert record["record_ref"].endswith("pullrequestreview-4242")


async def test_changes_requested_carries_the_posted_findings(fake: FakeScrutare, github: FakeGitHub) -> None:
    service = _service(fake, github)
    accepted = await service.dispatch_card(dict(CARD))
    fake.post(_job_of(accepted["session_id"]), verdict="changes_requested", head=HEAD, body="Two problems.")
    github.reviews = [
        {"author_login": "alice", "commit_oid": HEAD, "body": "human", "comments": [{"body": "not this"}]},
        {
            "author_login": LOGIN, "commit_oid": HEAD, "body": "Two problems.",
            "comments": [
                {"body": "Off by one.", "path": "src/a.py", "line": 12},
                {"body": "Missing test.", "path": "tests/test_a.py", "line": None},
            ],
        },
    ]
    status = await service.check_status(accepted["session_id"])
    assert status["status"] == "changes_requested"
    assert status["body"] == "Two problems."
    assert status["head_sha"] == HEAD
    assert [c["body"] for c in status["comments"]] == ["Off by one.", "Missing test."]
    assert status["comments"][0]["path"] == "src/a.py"
    assert status["comments"][0]["line"] == 12
    assert status["comments"][0]["author_login"] == LOGIN


async def test_escalated_needs_a_human(fake: FakeScrutare, github: FakeGitHub) -> None:
    service = _service(fake, github)
    accepted = await service.dispatch_card(dict(CARD))
    fake.post(_job_of(accepted["session_id"]), verdict="escalated", head=HEAD)
    status = await service.check_status(accepted["session_id"])
    assert status["status"] == "blocked"
    assert "escalated" in status["questions"][0]
    assert "pullrequestreview-4242" in status["questions"][0]


async def test_a_result_for_an_older_head_is_refused(fake: FakeScrutare, github: FakeGitHub) -> None:
    service = _service(fake, github)
    accepted = await service.dispatch_card(dict(CARD))
    fake.post(_job_of(accepted["session_id"]), verdict="approve", head=HEAD)
    github.head = NEXT  # someone pushed while the review ran
    with pytest.raises(TransportError, match="result is refused"):
        await service.check_status(accepted["session_id"])


@pytest.mark.parametrize("status", ["failed", "interrupted", "not_started"])
async def test_a_job_that_sent_nothing_is_a_retryable_error(
    fake: FakeScrutare, github: FakeGitHub, status: str,
) -> None:
    service = _service(fake, github)
    accepted = await service.dispatch_card(dict(CARD))
    fake.finish(_job_of(accepted["session_id"]), status, {"delivery": "not_sent", "reason": "model down"})
    with pytest.raises(TransportError, match="model down"):
        await service.check_status(accepted["session_id"])


async def test_a_job_that_may_have_posted_blocks_instead_of_resubmitting(fake: FakeScrutare, github: FakeGitHub) -> None:
    service = _service(fake, github)
    accepted = await service.dispatch_card(dict(CARD))
    fake.finish(_job_of(accepted["session_id"]), "interrupted", {"delivery": "uncertain"})
    status = await service.check_status(accepted["session_id"])
    assert status["status"] == "blocked"
    assert "posting.json" in status["questions"][0]
    # Polling again never resubmits: the block stands until a person acts.
    assert (await service.check_status(accepted["session_id"]))["status"] == "blocked"
    assert fake.created == 1


@pytest.mark.parametrize("ending", ["uncertain", "confirmed", "escalated"])
async def test_a_person_sending_a_blocked_card_back_gets_a_fresh_review(
    fake: FakeScrutare, github: FakeGitHub, ending: str,
) -> None:
    """A blocked card is dispatched again only after a person unblocks it."""
    service = _service(fake, github)
    accepted = await service.dispatch_card(dict(CARD))
    job = _job_of(accepted["session_id"])
    if ending == "escalated":
        fake.post(job, verdict="escalated", head=HEAD)
    else:
        fake.finish(job, "failed", {"delivery": ending})
    assert (await service.check_status(accepted["session_id"]))["status"] == "blocked"
    again = await service.dispatch_card(dict(CARD))
    assert _job_of(again["session_id"]) != job
    assert fake.by_request[_request().request_id(2)] == _job_of(again["session_id"])
    assert fake.created == 2


async def test_findings_match_the_graphql_form_of_the_bot_login(fake: FakeScrutare, github: FakeGitHub) -> None:
    """REST names the bot ``slug[bot]``; GraphQL's author.login is ``slug``."""
    service = _service(fake, github)
    accepted = await service.dispatch_card(dict(CARD))
    fake.post(_job_of(accepted["session_id"]), verdict="changes_requested", head=HEAD)
    github.reviews = [{
        "author_login": LOGIN.removesuffix("[bot]"), "commit_oid": HEAD, "body": "b",
        "comments": [{"body": "Off by one.", "path": "a.py", "line": 3}],
    }]
    status = await service.check_status(accepted["session_id"])
    assert [c["body"] for c in status["comments"]] == ["Off by one."]


async def test_feedback_queued_for_a_provider_is_reported_not_silently_dropped(
    fake: FakeScrutare, github: FakeGitHub,
) -> None:
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        await _service(fake, github).dispatch_card({**CARD, "relay_feedback": [{"body": "look again"}]})
    assert any(e["event"] == "stage_provider.feedback_not_forwarded" and e["items"] == 1 for e in logs)


async def test_scrutare_unreachable_is_a_retryable_error_at_dispatch_and_poll(
    fake: FakeScrutare, github: FakeGitHub,
) -> None:
    service = _service(fake, github)
    accepted = await service.dispatch_card(dict(CARD))
    fake.up = False
    with pytest.raises(TransportError):
        await service.check_status(accepted["session_id"])
    with pytest.raises(TransportError):
        await service.dispatch_card(dict(CARD))
    health = await service.check_health()
    assert health["status"] == "unhealthy"


async def test_github_unreadable_is_a_retryable_error(fake: FakeScrutare, github: FakeGitHub) -> None:
    github.fail = True
    with pytest.raises(TransportError, match="GitHub"):
        await _service(fake, github).dispatch_card(dict(CARD))
    assert fake.created == 0


async def test_a_card_without_a_pull_request_is_not_submitted(fake: FakeScrutare, github: FakeGitHub) -> None:
    with pytest.raises(TransportError, match="no pull request"):
        await _service(fake, github).dispatch_card({"id": "PVTI_card1"})
    assert fake.created == 0


async def test_an_unrecognised_session_is_a_retryable_error(fake: FakeScrutare, github: FakeGitHub) -> None:
    with pytest.raises(TransportError, match="unrecognised session"):
        await _service(fake, github).check_status("not-a-session")


# -- the acceptance scenarios ----------------------------------------------------------------


async def test_restart_during_a_review_resumes_the_same_job(fake: FakeScrutare, github: FakeGitHub) -> None:
    """A new daemon (new service object) re-dispatching the card finds the running job."""
    before = await _service(fake, github).dispatch_card(dict(CARD))
    restarted = _service(fake, github)
    # Resumed by polling the persisted session ...
    assert (await restarted.check_status(before["session_id"]))["status"] == "working"
    # ... or by a fresh dispatch: the request id is the same, so the job is the same.
    after = await restarted.dispatch_card(dict(CARD))
    assert after["session_id"] == before["session_id"]
    assert fake.created == 1
    fake.post(_job_of(after["session_id"]), verdict="approve", head=HEAD)
    assert (await restarted.check_status(after["session_id"]))["status"] == "approved"
    assert fake.created == 1


async def test_one_review_per_head_and_the_next_head_is_reviewed_again(fake: FakeScrutare, github: FakeGitHub) -> None:
    service = _service(fake, github)
    first = await service.dispatch_card(dict(CARD))
    fake.post(_job_of(first["session_id"]), verdict="changes_requested", head=HEAD)
    github.reviews = [{"author_login": LOGIN, "commit_oid": HEAD, "body": "fix it", "comments": []}]
    assert (await service.check_status(first["session_id"]))["status"] == "changes_requested"
    # The implementer pushes; the next head is a new request and a new review.
    github.head = NEXT
    second = await service.dispatch_card(dict(CARD))
    assert _job_of(second["session_id"]) != _job_of(first["session_id"])
    assert fake.created == 2
    fake.post(_job_of(second["session_id"]), verdict="approve", head=NEXT)
    status = await service.check_status(second["session_id"])
    assert status["status"] == "approved"
    assert status["head_sha"] == NEXT


async def test_a_retry_after_a_failure_that_sent_nothing_uses_the_next_attempt(
    fake: FakeScrutare, github: FakeGitHub,
) -> None:
    service = _service(fake, github)
    first = await service.dispatch_card(dict(CARD))
    fake.finish(_job_of(first["session_id"]), "failed", {"delivery": "not_sent"})
    second = await service.dispatch_card(dict(CARD))
    assert _job_of(second["session_id"]) != _job_of(first["session_id"])
    assert fake.by_request[_request().request_id(2)] == _job_of(second["session_id"])
    # A third dispatch while attempt 2 runs finds attempt 2, not a new job.
    third = await service.dispatch_card(dict(CARD))
    assert third["session_id"] == second["session_id"]
    assert fake.created == 2


async def test_attempts_are_bounded() -> None:
    class AlwaysSpent:
        name = "spent"

        async def submit(self, request: StageRequest, request_id: str) -> StageJob:
            return StageJob(ref=request_id, state=JobState.RETRYABLE, reason="nothing sent")

        async def poll(self, ref: str) -> StageJob:  # pragma: no cover - not reached
            raise AssertionError

        async def health(self) -> tuple[bool, str]:  # pragma: no cover - not reached
            return True, ""

    service = ExternalStageService(AlwaysSpent(), stage="reviewing", pr_reader=FakeGitHub().read)
    with pytest.raises(TransportError, match="attempts"):
        await service.dispatch_card(dict(CARD))


def test_only_contracted_stages_can_be_served() -> None:
    with pytest.raises(ValueError, match="no stage provider contract"):
        ExternalStageService(object(), stage="implementing", pr_reader=FakeGitHub().read)  # type: ignore[arg-type]


async def test_relay_feedback_is_refused_plainly(fake: FakeScrutare, github: FakeGitHub) -> None:
    reply = await _service(fake, github).relay_feedback({"pr_url": "x"})
    assert reply["status"] == "error"


# -- through the real monitor node ----------------------------------------------------------


def _monitored_state(service: ExternalStageService, session_id: str) -> dict[str, Any]:
    from types import SimpleNamespace

    from coordinare.graph.state import initial_state

    state = initial_state()
    state.update(
        performer_services={"reviewing": object()},
        performer_services_by_id={service.performer_id: service},
        performer_stage="reviewing",
        lifecycle_sequence=["implementing", "reviewing", "documenting"],
        current_card={
            "id": "PVTI_card1", "status": "IN_PROGRESS", "title": "Widget",
            "pr_url": CARD["pr_url"], "pr_node_id": CARD["pr_node_id"], "pr_number": 7,
        },
        agent_dispatch={"session_id": session_id, "performer_id": service.performer_id},
        config=SimpleNamespace(max_feedback_cycles=5),
    )
    return state


async def test_monitor_routes_scrutare_findings_to_the_implementer(fake: FakeScrutare, github: FakeGitHub) -> None:
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    service = _service(fake, github)
    accepted = await service.dispatch_card(dict(CARD))
    fake.post(_job_of(accepted["session_id"]), verdict="changes_requested", head=HEAD, body="Two problems.")
    github.reviews = [{
        "author_login": LOGIN, "commit_oid": HEAD, "body": "Two problems.",
        "comments": [{"body": "Off by one.", "path": "src/a.py", "line": 12}],
    }]
    result = await monitor_performer(_monitored_state(service, accepted["session_id"]))  # type: ignore[arg-type]
    assert result["performer_stage"] == "implementing"
    assert result["phase"] == "dispatching"
    bodies = " ".join(str(item.get("body")) for item in result["relay_feedback"])
    assert "Off by one." in bodies
    assert "Two problems." in bodies
    assert "reviewing" not in (result.get("stage_verdicts") or {})


async def test_monitor_records_scrutare_approval_against_the_reviewed_head(fake: FakeScrutare, github: FakeGitHub) -> None:
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    service = _service(fake, github)
    accepted = await service.dispatch_card(dict(CARD))
    fake.post(_job_of(accepted["session_id"]), verdict="approve", head=HEAD)
    result = await monitor_performer(_monitored_state(service, accepted["session_id"]))  # type: ignore[arg-type]
    verdict = result["stage_verdicts"]["reviewing"]
    assert verdict["head_sha"] == HEAD
    assert verdict["verdict"] == "approved"
    assert result["performer_stage"] == "documenting"


@pytest.mark.parametrize("ending", ["moved_head", "failed", "unreachable"])
async def test_monitor_treats_a_refused_or_failed_review_as_a_system_error(
    fake: FakeScrutare, github: FakeGitHub, ending: str,
) -> None:
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    service = _service(fake, github)
    accepted = await service.dispatch_card(dict(CARD))
    job = _job_of(accepted["session_id"])
    if ending == "moved_head":
        fake.post(job, verdict="approve", head=HEAD)
        github.head = NEXT
    elif ending == "failed":
        fake.finish(job, "failed", {"delivery": "not_sent"})
    else:
        fake.up = False
    result = await monitor_performer(_monitored_state(service, accepted["session_id"]))  # type: ignore[arg-type]
    assert result["phase"] == "system_error"
    assert "reviewing" not in (result.get("stage_verdicts") or {})


async def test_monitor_keeps_waiting_while_the_job_runs(fake: FakeScrutare, github: FakeGitHub) -> None:
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    service = _service(fake, github)
    accepted = await service.dispatch_card(dict(CARD))
    fake.finish(_job_of(accepted["session_id"]), "running", None)
    result = await monitor_performer(_monitored_state(service, accepted["session_id"]))  # type: ignore[arg-type]
    assert result["phase"] == "monitoring_performer"


async def test_startup_reconciliation_keeps_a_provider_session_for_polling(
    fake: FakeScrutare, github: FakeGitHub,
) -> None:
    """A restart neither reaps nor re-dispatches the review: it polls the same job next."""
    from coordinare.services.reconciliation import (
        ReconciliationDecision,
        run_startup_reconciliation,
    )

    service = _service(fake, github)
    accepted = await service.dispatch_card(dict(CARD))

    class NoDocker:
        async def list_containers_by_label(self, *args: Any, **kwargs: Any) -> list[Any]:
            raise AssertionError("a provider session needs no container lookup")

    dispatch = {"session_id": accepted["session_id"], "performer_id": service.performer_id}
    state: dict[str, Any] = {
        "performer_services": {"reviewing": service},
        "performer_services_by_id": {service.performer_id: service},
        "active_sessions": {
            "PVTI_card1": {
                "phase": "monitoring_performer", "performer_stage": "reviewing",
                "agent_dispatch": dict(dispatch), "current_card": dict(CARD),
            },
        },
    }
    report = await run_startup_reconciliation(state, NoDocker())  # type: ignore[arg-type]
    assert report.decisions["PVTI_card1"] in (
        ReconciliationDecision.DEFERRED, ReconciliationDecision.SKIPPED_PERSISTENT,
    )
    assert state["active_sessions"]["PVTI_card1"]["agent_dispatch"] == dispatch
    assert fake.created == 1
