"""Repair improves proposals without weakening independent review or required checks."""
from __future__ import annotations

import asyncio
import copy
import json

import pytest
from coordinare_service_inference.environment.models import EnvironmentManifest

from coordinare.services.environment_attempt import AttemptBudget, DiscoveryChatReply
from coordinare.services.environment_discovery import EnvironmentDiscoveryError
from tests.unit.scope_chat_fixture import discover_with_fixture_scope as discover_and_review
from tests.unit.test_351_environment_discovery import (
    CAPABILITIES,
    COMMIT,
    PATH,
    SOURCE,
)
from tests.unit.test_351_environment_discovery import (
    candidate as full_candidate,
)


def candidate():
    from tests.unit.env_manifest_fixture import manifest_proposal
    return manifest_proposal(full_candidate())


class Replies:
    def __init__(self, values):
        self.values = iter(values)
        self.messages = []

    async def __call__(self, messages):
        self.messages.append(copy.deepcopy(messages))
        value = next(self.values)
        return json.dumps(value)


def finding_responses():
    return [{'finding_id': 'F0001', 'rationale': 'Resolved using source', 'sources': [PATH], 'affected_ids': []}]


def assessment(manifest, verdict="accept", *, retained=False):
    return {"assessment": {"verdict": verdict,
        "rationale": "Review source for required executable coverage", "sources": [PATH],
        'capability_unavailable': False,
        'findings': ([{'category': 'coverage', 'issue': 'Required executable coverage is incomplete',
                      'sources': [PATH], 'affected_ids': []}] if verdict == 'revise' and not retained else []),
        'decisions': ([{'finding_id': 'F0001', 'status': 'resolved' if verdict == 'accept' else 'open',
                       'rationale': 'Independent source decision', 'sources': [PATH], 'duplicate_of': None}] if retained else [])}}


async def run(chat, **kwargs):
    async def fetch(path):
        return SOURCE

    return await discover_and_review(symphony_name="suite", commit=COMMIT, paths=[PATH, "other/evidence"],
        fetch=fetch, chat=chat, capabilities=CAPABILITIES, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["reference", "install_dependency"])
async def test_invalid_refs_then_valid_revision_fresh_review(kind):
    valid = candidate()
    invalid = copy.deepcopy(valid)
    if kind == "reference":
        invalid["requirements"][0]["checks"] = ["zog --version"]
    else:
        invalid["install"] = [{"id": "install-dev", "argv": ["true"]}]
        valid["install"] = copy.deepcopy(invalid["install"])
        invalid["checks"][0]["depends_on"] = ["install-dev"]
    replies = Replies([{"tool": "read_file", "path": PATH}, {"manifest": invalid}, {"manifest": valid},
        {"tool": "read_file", "path": PATH}, assessment(valid)])
    events = []
    manifest, review = await run(replies, diagnostics=events.append)
    assert review.manifest_digest == manifest.digest()
    assert manifest.requirements[0].checks == ["version"]
    feedback = replies.messages[2][-1]
    assert feedback["role"] == "user"
    errors = json.loads(feedback["content"])["validation_errors"]
    assert errors[0]["code"] == ("requirement_check_reference" if kind == "reference" else "clean_dependency_install_only")
    reviewer_start = replies.messages[3]
    assert len(reviewer_start) == 2
    assert invalid not in [json.loads(message["content"]) for message in reviewer_start if message["role"] == "user"]
    assert events[-1]["repairs"] == 1
    assert events[-1]["outcome"] == "accepted"


@pytest.mark.asyncio
async def test_revision_requires_new_independent_conversation_and_digest():
    original = candidate()
    revised = copy.deepcopy(original)
    revised["checks"][0]["argv"].append("--strict")
    replies = Replies([{"tool": "read_file", "path": PATH}, {"manifest": original},
        {"tool": "read_file", "path": PATH}, assessment(original, "revise"), {"manifest": revised, "finding_responses": finding_responses()},
        {"tool": "read_file", "path": PATH}, assessment(revised, retained=True)])
    manifest, review = await run(replies)
    assert manifest.digest() == review.manifest_digest != EnvironmentManifest.model_validate(full_candidate()).digest()
    revision_feedback = json.loads(replies.messages[4][-1]["content"])
    assert revision_feedback["coverage_revision"]["required_ids"] == {
        group: sorted(item["id"] for item in original[group] if item.get("required", True))
        for group in ("requirements", "install", "checks", "services")
    }
    assert "never rename" in revision_feedback["instruction"].lower()
    assert len(replies.messages[5]) == 2
    assert not any(message["role"] == "assistant" for message in replies.messages[5])


@pytest.mark.asyncio
async def test_third_repair_held():
    calls = []

    async def unsupported(messages):
        calls.append(messages)
        return "{}"

    events = []
    with pytest.raises(EnvironmentDiscoveryError, match="repair limit"):
        await run(unsupported, diagnostics=events.append)
    assert len(calls) == 3
    assert events[-1]["repairs"] == 2


@pytest.mark.asyncio
async def test_agent_source_identity_is_forbidden_and_never_adopted():
    invalid = candidate()
    invalid["source_commit"] = "b" * 40
    replies = Replies([{"tool": "read_file", "path": PATH}, *[{"manifest": invalid}] * 3])
    events = []
    with pytest.raises(EnvironmentDiscoveryError, match="repair limit"):
        await run(replies, diagnostics=events.append)
    assert events[-1]["outcome"] == "held"
    assert 'extra_forbidden' in events[-1]['validation_codes']


@pytest.mark.asyncio
async def test_secret_validation_values_not_logged():
    valid = candidate()
    invalid = copy.deepcopy(valid)
    invalid["secret-private-value"] = "private-value"
    replies = Replies([{"tool": "read_file", "path": PATH}, {"manifest": invalid}, {"manifest": valid},
        {"tool": "read_file", "path": PATH}, assessment(valid)])
    events = []
    await run(replies, diagnostics=events.append)
    feedback = replies.messages[2][-1]["content"]
    assert "private-value" not in feedback
    assert "private-value" not in json.dumps(events)


@pytest.mark.asyncio
async def test_aggregate_deadline_cancels_backend_work(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    budget = AttemptBudget()
    original_deadline = budget.deadline
    aggregate_timer = None
    timeout_at = asyncio.timeout_at

    def capture_aggregate_timer(deadline):
        nonlocal aggregate_timer
        timer = timeout_at(deadline)
        if deadline == original_deadline and aggregate_timer is None:
            aggregate_timer = timer
        return timer

    monkeypatch.setattr(asyncio, 'timeout_at', capture_aggregate_timer)

    async def slow(messages):
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    operation = asyncio.create_task(run(slow, budget=budget))
    entered = asyncio.create_task(started.wait())
    try:
        await asyncio.wait({operation, entered}, return_when=asyncio.FIRST_COMPLETED)
        assert started.is_set(), 'discovery stopped before entering the backend'
        assert aggregate_timer is not None
        # Expire the real aggregate timeout only after there is work to cancel.
        # Scope/schema setup speed under coverage must not choose the test path.
        aggregate_timer.reschedule(asyncio.get_running_loop().time())
        with pytest.raises(EnvironmentDiscoveryError) as failure:
            await operation
        assert failure.value.code == 'attempt_deadline'
        assert cancelled.is_set()
        assert budget.deadline == original_deadline
    finally:
        operation.cancel()
        entered.cancel()
        await asyncio.gather(operation, entered, return_exceptions=True)


def test_aggregate_read_and_call_budgets_do_not_reset():
    budget = AttemptBudget(timeout_seconds=600, max_calls=2, max_reads=2, max_schema_repairs=2)
    budget.reserve_call()
    budget.reserve_repair("schema")
    budget.reserve_call()
    with pytest.raises(EnvironmentDiscoveryError, match="call limit"):
        budget.reserve_call()
    budget.reserve_reads(2)
    with pytest.raises(EnvironmentDiscoveryError, match="read limit"):
        budget.reserve_reads(1)


@pytest.mark.asyncio
async def test_provider_metadata_optional_and_request_local():
    valid = candidate()
    scripted = Replies([{"tool": "read_file", "path": PATH}, {"manifest": valid},
        {"tool": "read_file", "path": PATH}, assessment(valid)])
    events = []

    async def metadata(messages):
        return DiscoveryChatReply(text=await scripted(messages), usage={"input": 3, "output": 2}, finish_reason="stop")

    await run(metadata, diagnostics=events.append)
    assert events[-1]["usage"] == {"input": 12, "output": 8}
    assert events[-1]["finish_reasons"] == {"stop": 4}
    assert events[-1]["metadata_available"] is True


@pytest.mark.asyncio
async def test_review_cannot_remove_obligation_without_evidence():
    invalid = candidate()
    invalid["requirements"][0]["checks"] = ["not-a-check"]
    omitted = copy.deepcopy(candidate())
    omitted["requirements"] = []
    omitted["empty_reason"] = "Discard the requirement to pass validation"
    replies = Replies([{"tool": "read_file", "path": PATH}, {"manifest": invalid}, {"manifest": omitted}])
    with pytest.raises(EnvironmentDiscoveryError, match="obligation"):
        await run(replies)


@pytest.mark.asyncio
async def test_backend_metadata_remains_request_local(monkeypatch):
    import httpx

    from coordinare.services.conducting import OpenAiApiBackend

    def answer(request):
        label = json.loads(request.content)["messages"][0]["content"]
        count = 11 if label == "first" else 22
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"label": label})},
            "finish_reason": "stop" if label == "first" else "length"}],
            "usage": {"prompt_tokens": count, "completion_tokens": 2}})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(answer), **kwargs))
    backend = OpenAiApiBackend(api_key="test-reference", base_url="https://test.invalid/v1")
    first, second = await asyncio.gather(backend.chat_json_with_metadata([{"role": "user", "content": "first"}]),
        backend.chat_json_with_metadata([{"role": "user", "content": "second"}]))
    assert first.usage["input_tokens"] == 11
    assert second.usage["input_tokens"] == 22
    assert first.finish_reason == "stop"
    assert second.finish_reason == "length"
    assert json.loads(first.text) == {"label": "first"}
    assert isinstance(await backend.chat_json([{"role": "user", "content": "first"}]), str)


@pytest.mark.parametrize("changes", [{"max_schema_repairs": 9}, {"max_schema_repairs": -1}, {"timeout_seconds": 4 * 3600 + 1}, {"max_calls": 0}])
def test_invalid_attempt_bounds_cannot_extend_policy(changes):
    with pytest.raises(ValueError):
        AttemptBudget(**changes)


@pytest.mark.asyncio
async def test_schema_limit_is_independent_of_coverage_revision():
    valid = candidate()
    invalid = copy.deepcopy(valid)
    invalid["requirements"][0]["checks"] = ["not-a-check"]
    replies = Replies([{}, {"tool": "read_file", "path": PATH}, {"manifest": valid},
        {"tool": "read_file", "path": PATH}, assessment(valid, "revise"), {"manifest": invalid}, {"manifest": invalid}])
    events = []
    with pytest.raises(EnvironmentDiscoveryError, match="repair limit"):
        await run(replies, diagnostics=events.append)
    assert len(replies.messages) == 7
    assert events[-1]["schema_repairs"] == 2
    assert events[-1]["coverage_revisions"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", [
    {"tool": "read_files", "paths": ["other"]},
    {"tool": "list_files", "path": "absent", "cursor": 0},
    {"tool": "list_files", "path": "", "cursor": "next"},
])
async def test_invalid_navigation_does_not_spend_schema_repairs(tool):
    valid = candidate()
    replies = Replies([tool, {"tool": "read_file", "path": PATH}, {"manifest": valid},
        {"tool": "read_file", "path": PATH}, assessment(valid)])
    events = []
    manifest, review = await run(replies, diagnostics=events.append)
    assert manifest.inputs == full_candidate()["inputs"]
    assert review.verdict == "accept"
    assert events[-1]["repairs"] == 0
    assert events[-1]["reads"] == 6
    feedback = json.loads(replies.messages[1][-1]["content"])
    assert feedback["tool_result"]["errors"]


@pytest.mark.asyncio
async def test_unavailable_snapshot_file_cannot_establish_scope_evidence():
    from coordinare.services.environment_discovery import discover_and_review as discover
    from tests.unit.scope_chat_fixture import ScopeChatFixture

    async def unavailable(path):
        return None

    replies = Replies([{"tool": "read_file", "path": PATH}])
    scope_chat = ScopeChatFixture(replies, PATH)
    events = []
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        await discover(symphony_name="suite", commit=COMMIT, paths=[PATH],
            fetch=unavailable, chat=scope_chat, capabilities=CAPABILITIES, diagnostics=events.append)
    assert exc.value.code == 'scope_phase_call_limit'
    assert events[-1]["repairs"] == 0
    assert events[-1]["outcome"] == 'held'
    assert replies.messages == []  # Unavailable evidence never reaches construction or review.
    feedback = json.loads(scope_chat.scope_messages[-1][-1]['content'])['tool_result']
    assert feedback['errors'] == [{'index': 0, 'code': 'source_unavailable'}]
    assert feedback['files'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["discovery", "review"])
async def test_missing_declaration_fields_are_required_in_proposal_schema_and_repairable(phase):
    valid = candidate()
    invalid = copy.deepcopy(valid if phase == "discovery" else assessment(valid)["assessment"])
    missing = "requirements" if phase == "discovery" else "sources"
    invalid.pop(missing)
    if phase == "discovery":
        values = [{"tool": "read_file", "path": PATH}, {"manifest": invalid}, {"manifest": valid},
            {"tool": "read_file", "path": PATH}, assessment(valid)]
        feedback_index = 2
        schema_index = 0
    else:
        values = [{"tool": "read_file", "path": PATH}, {"manifest": valid},
            {"tool": "read_file", "path": PATH}, {"assessment": invalid}, assessment(valid)]
        feedback_index = 4
        schema_index = 2
    replies = Replies(values)
    events = []
    manifest, review = await run(replies, diagnostics=events.append)
    assert manifest.capabilities_digest == review.capabilities_digest == CAPABILITIES.digest()
    errors = json.loads(replies.messages[feedback_index][-1]["content"])["validation_errors"]
    assert {tuple(error["location"]) for error in errors} == {(missing,)}
    assert {error["code"] for error in errors} == {"missing"}
    prompt = replies.messages[schema_index][0]["content"]
    schema = json.loads(prompt.rsplit("matching this schema: ", 1)[1])
    if phase == "review":
        assert "assessment" in schema["required"] and schema["additionalProperties"] is False
        schema = schema["$defs"]["CoverageAssessmentProposal"]
    else:
        schema = schema["$defs"]["EnvironmentManifestProposal"]
    assert missing in schema["required"]
    assert not {"inventory_digest", "capabilities_digest", "source_commit", "inputs"} & set(schema["properties"])
    assert events[-1]["repairs"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["inventory_digest", "capabilities_digest"])
async def test_supplied_proposal_identity_is_rejected_as_extra(field):
    invalid = candidate()
    invalid[field] = "b" * 64
    replies = Replies([{"tool": "read_file", "path": PATH}, {"manifest": invalid}, {"manifest": candidate()},
        {"tool": "read_file", "path": PATH}, assessment(candidate())])
    events = []
    manifest, review = await run(replies, diagnostics=events.append)
    assert getattr(manifest, field) == getattr(review, field) != "b" * 64
    assert events[-1]["repairs"] == 1
    assert 'extra_forbidden' in events[-1]['validation_codes']


@pytest.mark.asyncio
async def test_bare_manifest_and_schema_echo_review_repair_with_explicit_envelopes():
    valid = candidate()
    calls = []

    async def protocol_model(messages):
        calls.append(copy.deepcopy(messages))
        context = json.loads(messages[1]["content"])
        reviewing = "manifest" in context
        if len(messages) == 2:
            return json.dumps({"tool": "read_file", "path": PATH})
        last = json.loads(messages[-1]["content"])
        if "validation_errors" in last:
            expected = last["expected_envelope"]
            assert expected == ("assessment" if reviewing else "manifest")
            body = assessment(valid)["assessment"] if reviewing else valid
            return json.dumps({expected: body})
        if reviewing:
            return json.dumps({"assessment": {"properties": {}}, **assessment(valid)["assessment"]})
        return json.dumps(valid)

    events = []
    manifest, review = await run(protocol_model, diagnostics=events.append)
    assert review.manifest_digest == manifest.digest()
    assert events[-1]["repairs"] == 2
    assert sum(len(messages) == 2 for messages in calls) == 2


@pytest.mark.asyncio
async def test_provider_error_is_not_retried_as_malformed_json(monkeypatch):
    import httpx

    from coordinare.services.conducting import OpenAiApiBackend
    original = httpx.AsyncClient
    def answer(request):
        return httpx.Response(400, json={'error': {'code':'context_length_exceeded', 'message':'request rejected'}})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(answer), **kwargs))
    backend = OpenAiApiBackend(api_key='fixture', base_url='https://test.invalid/v1')
    events = []
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        await run(backend.chat_json_with_metadata, diagnostics=events.append)
    assert exc.value.code == 'provider_request_failed'
    assert events[-1]['calls'] == 4
    assert events[-1]['schema_repairs'] == 0


@pytest.mark.asyncio
async def test_navigation_recovery_survives_exhausted_schema_allowance():
    valid = candidate()
    replies = Replies([{}, {}, {'tool': 'list_files', 'path': '.'},
        {'tool': 'read_files', 'paths': ['missing', PATH]}, {'manifest': valid},
        {'tool': 'read_file', 'path': PATH}, assessment(valid)])
    events = []
    manifest, review = await run(replies, diagnostics=events.append)
    assert review.manifest_digest == manifest.digest()
    assert events[-1]['schema_repairs'] == 2
    assert 'missing' not in manifest.inputs


@pytest.mark.asyncio
async def test_repeated_navigation_errors_still_exhaust_aggregate_calls():
    replies = Replies([{'tool': 'read_file', 'path': 'absent'}] * 3)
    budget = AttemptBudget(timeout_seconds=600, max_calls=5, max_reads=5, max_schema_repairs=2)
    with pytest.raises(EnvironmentDiscoveryError, match='call limit'):
        await run(replies, budget=budget)
    assert budget.schema_repairs == 0
    assert len(replies.messages) == 2
