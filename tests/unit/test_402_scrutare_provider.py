"""402: the Scrutare provider against a fake of Scrutare's performer API."""
from __future__ import annotations

import json

import pytest

from coordinare.services.scrutare import ScrutareProvider, job_from_record
from coordinare.services.stage_contract import JobState, StageRequest, StageVerdict
from coordinare.stage_provider_config import ScrutareProviderConfig
from coordinare.transport.base import TransportError
from coordinare.transport.http_transport import PerformerAuthError
from tests.unit.fake_scrutare import LOGIN, TOKEN, FakeScrutare, posted_result

HEAD = "a" * 40
OTHER = "b" * 40


def _config(**kw: object) -> ScrutareProviderConfig:
    base: dict[str, object] = {
        "kind": "scrutare", "url": "http://scrutare.test", "token_env": "SCRUTARE_TEST_TOKEN",
        "installation": 169458182, "review_login": LOGIN,
    }
    base.update(kw)
    return ScrutareProviderConfig.model_validate(base)


def _request(head: str = HEAD, number: int = 7) -> StageRequest:
    return StageRequest(
        card_id="PVTI_card1", stage="reviewing", repository="acme/widgets",
        pr_number=number, pr_node_id="PR_node7", head_sha=head,
    )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeScrutare:
    monkeypatch.setenv("SCRUTARE_TEST_TOKEN", TOKEN)
    return FakeScrutare()


def _provider(fake: FakeScrutare, **kw: object) -> ScrutareProvider:
    return ScrutareProvider("scrutare", _config(**kw), transport=fake.transport)


@pytest.mark.asyncio
async def test_submit_sends_the_documented_request_with_a_bearer_token(fake: FakeScrutare) -> None:
    job = await _provider(fake).submit(_request(), "rid-1")
    sent = fake.requests[-1]
    assert sent.method == "POST"
    assert sent.url.path == "/api/reviews"
    assert sent.headers["Authorization"] == f"Bearer {TOKEN}"
    assert json.loads(sent.content) == {
        "repository": "acme/widgets", "number": 7, "installation": 169458182, "request_id": "rid-1",
    }
    assert job.state is JobState.PENDING
    assert job.detail == "queued"


@pytest.mark.asyncio
async def test_the_same_request_id_finds_the_same_job(fake: FakeScrutare) -> None:
    provider = _provider(fake)
    first = await provider.submit(_request(), "rid-1")
    again = await provider.submit(_request(), "rid-1")
    assert first.ref == again.ref
    assert fake.created == 1


@pytest.mark.asyncio
async def test_a_request_id_reused_for_another_target_is_an_error_not_a_job(fake: FakeScrutare) -> None:
    provider = _provider(fake)
    await provider.submit(_request(), "rid-1")
    with pytest.raises(TransportError, match="409"):
        await provider.submit(_request(number=8), "rid-1")


@pytest.mark.asyncio
async def test_a_refused_token_is_a_configuration_error(fake: FakeScrutare) -> None:
    fake.token = "something-else"
    with pytest.raises(PerformerAuthError, match="SCRUTARE_TEST_TOKEN"):
        await _provider(fake).submit(_request(), "rid-1")


@pytest.mark.asyncio
async def test_an_unset_token_is_a_configuration_error(fake: FakeScrutare, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCRUTARE_TEST_TOKEN")
    with pytest.raises(PerformerAuthError, match="SCRUTARE_TEST_TOKEN is not set"):
        await _provider(fake).submit(_request(), "rid-1")
    assert fake.requests == []


@pytest.mark.asyncio
async def test_unreachable_scrutare_is_a_transport_error(fake: FakeScrutare) -> None:
    fake.up = False
    with pytest.raises(TransportError, match="unreachable"):
        await _provider(fake).submit(_request(), "rid-1")
    with pytest.raises(TransportError, match="unreachable"):
        await _provider(fake).poll("0123abcd")


@pytest.mark.asyncio
async def test_poll_reads_the_job_and_maps_a_posted_review(fake: FakeScrutare) -> None:
    provider = _provider(fake)
    job = await provider.submit(_request(), "rid-1")
    fake.post(job.ref, verdict="approve", head=HEAD, body="Looks right.")
    done = await provider.poll(job.ref)
    assert done.state is JobState.DONE
    assert done.outcome is not None
    assert done.outcome.verdict is StageVerdict.PASSED
    assert done.outcome.reviewed_head == HEAD
    assert done.outcome.summary == "Looks right."
    assert done.outcome.author_login == LOGIN
    assert done.outcome.record_ref.endswith("pullrequestreview-4242")


@pytest.mark.asyncio
async def test_poll_of_an_unknown_job_is_retryable(fake: FakeScrutare) -> None:
    with pytest.raises(TransportError, match="404"):
        await _provider(fake).poll("deadbeef")


@pytest.mark.asyncio
async def test_health_reports_reachable_and_token_accepted(fake: FakeScrutare) -> None:
    ok, detail = await _provider(fake).health()
    assert ok, detail


@pytest.mark.asyncio
async def test_health_refuses_a_404_that_is_not_scrutares(fake: FakeScrutare) -> None:
    import httpx

    def ingress(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404, text="not found")

    provider = ScrutareProvider("scrutare", _config(), transport=httpx.MockTransport(ingress))
    ok, detail = await provider.health()
    assert not ok
    assert "not Scrutare's performer API" in detail


@pytest.mark.asyncio
async def test_health_names_a_refused_token(fake: FakeScrutare) -> None:
    fake.token = "something-else"
    ok, detail = await _provider(fake).health()
    assert not ok
    assert "SCRUTARE_TEST_TOKEN" in detail


@pytest.mark.parametrize(
    ("status", "verdict", "state", "contract"),
    [
        ("posted", "approve", JobState.DONE, StageVerdict.PASSED),
        ("posted", "changes_requested", JobState.DONE, StageVerdict.CHANGES_REQUESTED),
        ("posted", "escalated", JobState.DONE, StageVerdict.NEEDS_HUMAN),
    ],
)
def test_verdicts_map_onto_the_contract(status: str, verdict: str, state: JobState, contract: StageVerdict) -> None:
    job = job_from_record({"id": "j1", "status": status, "result": posted_result(verdict=verdict, head=HEAD)})
    assert job.state is state
    assert job.outcome is not None
    assert job.outcome.verdict is contract


@pytest.mark.parametrize("status", ["queued", "running"])
def test_queued_and_running_are_pending(status: str) -> None:
    assert job_from_record({"id": "j1", "status": status, "result": None}).state is JobState.PENDING


@pytest.mark.parametrize("status", ["failed", "interrupted", "not_started"])
def test_an_end_that_sent_nothing_is_retryable(status: str) -> None:
    job = job_from_record({"id": "j1", "status": status, "result": {"delivery": "not_sent", "reason": "capture failed"}})
    assert job.state is JobState.RETRYABLE
    assert "capture failed" in job.reason


@pytest.mark.parametrize(("status", "delivery"), [
    ("failed", "confirmed"), ("interrupted", "uncertain"), ("failed", ""),
])
def test_an_end_that_may_have_delivered_is_never_retried(status: str, delivery: str) -> None:
    job = job_from_record({"id": "j1", "status": status, "result": {"delivery": delivery}})
    assert job.state is JobState.NEEDS_HUMAN
    assert job.reason


@pytest.mark.parametrize("delivery", ["confirmed", "uncertain"])
def test_a_superseded_job_that_may_have_posted_is_never_retried(delivery: str) -> None:
    job = job_from_record({"id": "j1", "status": "superseded", "result": {"delivery": delivery}})
    assert job.state is JobState.NEEDS_HUMAN


def test_a_superseded_job_that_never_ran_is_retryable() -> None:
    job = job_from_record({"id": "j1", "status": "superseded", "result": {"superseded_by": "j0"}})
    assert job.state is JobState.RETRYABLE


@pytest.mark.parametrize("damage", ["no_review", "no_url", "no_commit"])
def test_a_posted_job_without_a_head_bound_receipt_is_not_a_pass(damage: str) -> None:
    result = posted_result(verdict="approve", head=HEAD)
    if damage == "no_review":
        del result["review"]
    elif damage == "no_url":
        result["review"]["html_url"] = ""
    else:
        del result["review"]["commit_id"]
    job = job_from_record({"id": "j1", "status": "posted", "result": result})
    assert job.state is JobState.NEEDS_HUMAN
    assert "receipt" in job.reason


def test_a_review_posted_under_another_login_is_not_a_pass() -> None:
    result = posted_result(verdict="approve", head=HEAD, login="someone-else[bot]")
    job = job_from_record({"id": "j1", "status": "posted", "result": result}, review_login=LOGIN)
    assert job.state is JobState.NEEDS_HUMAN
    assert "review_login" in job.reason
    same = posted_result(verdict="approve", head=HEAD, login=LOGIN.removesuffix("[bot]"))
    assert job_from_record({"id": "j1", "status": "posted", "result": same}, review_login=LOGIN).state is JobState.DONE


def test_an_unusable_success_document_is_not_a_pass() -> None:
    job = job_from_record({"id": "j1", "status": "posted", "result": {"verdict": "approve"}})
    assert job.state is JobState.NEEDS_HUMAN


def test_a_receipt_on_another_commit_is_not_a_pass() -> None:
    result = posted_result(verdict="approve", head=HEAD)
    result["review"]["commit_id"] = OTHER
    assert job_from_record({"id": "j1", "status": "posted", "result": result}).state is JobState.NEEDS_HUMAN


def test_an_unknown_status_fails_closed() -> None:
    job = job_from_record({"id": "j1", "status": "archived", "result": None})
    assert job.state is JobState.NEEDS_HUMAN


def test_unchanged_without_a_verdict_is_not_a_pass() -> None:
    job = job_from_record({"id": "j1", "status": "unchanged", "result": {"reason": "replies are not enabled"}})
    assert job.state is JobState.NEEDS_HUMAN


def test_an_unchanged_result_whose_receipt_names_another_commit_is_not_a_pass() -> None:
    result = posted_result(verdict="approve", head=HEAD)
    result["status"] = "unchanged"
    result["review"]["commit_id"] = OTHER
    job = job_from_record({"id": "j1", "status": "unchanged", "result": result})
    assert job.state is JobState.NEEDS_HUMAN


def test_a_posted_escalation_without_a_receipt_still_reads_as_an_escalation() -> None:
    result = posted_result(verdict="escalated", head=HEAD)
    del result["review"]
    job = job_from_record({"id": "j1", "status": "posted", "result": result})
    assert job.state is JobState.DONE
    assert job.outcome is not None
    assert job.outcome.verdict is StageVerdict.NEEDS_HUMAN
