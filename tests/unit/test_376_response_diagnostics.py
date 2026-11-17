"""Pure review diagnostics share native corrections and never establish admission."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from typing import Any

import pytest
from nare import Reply, Usage

from coordinare.services.agent_runtime.checkpoint import CheckpointStore
from coordinare.services.agent_runtime.nare_runtime import NareRuntime
from coordinare.services.environment_attempt import AttemptBudget
from coordinare.services.environment_response_diagnostics import diagnostic_details
from tests.unit.test_376_agent_runtime import ScriptedTransport, request


def diagnose_candidate(candidate: object, envelope: str = 'scope_assessment') -> list[Any]:
    from coordinare.services.environment_response_diagnostics import review_response_diagnostics

    return review_response_diagnostics(candidate, envelope=envelope, inspected={'README.md': 'a' * 64})


@pytest.mark.parametrize('envelope', ['scope_assessment', 'assessment'])
def test_unicode_bounds_and_nested_citations_are_reported_without_mutation(envelope: str) -> None:
    candidate = {envelope: {'rationale': 'é' * 2049, 'sources': ['README.md'],
        'findings': [{'issue': 'é' * 1025, 'sources': ['private-draft-value']}],
        'decisions': [{'rationale': 'é' * 2049, 'sources': ['README.md', 'unread.md']}]}}
    before = deepcopy(candidate)
    issues = diagnose_candidate(candidate, envelope)
    assert candidate == before
    assert {item.code for item in issues} == {'response_utf8_limit',
        'scope_citations_invalid' if envelope == 'scope_assessment' else 'review_citations_invalid'}
    rendered = json.dumps([item.message for item in issues])
    # A reviewer's rationale is bounded at 4096 bytes and a finding's issue at 2048.
    assert '4098' in rendered and '4096' in rendered and '2050' in rendered and '2048' in rendered
    assert 'read_source' in rendered
    assert 'private-draft-value' not in rendered and 'unread.md' not in rendered


@pytest.mark.parametrize('candidate', [None, [], 3, {}, {'scope_assessment': None},
    {'scope_assessment': {'sources': [None, {}], 'findings': [None, 3], 'decisions': 'bad'}}])
def test_unexpected_shapes_are_left_to_strict_schema(candidate: object) -> None:
    assert diagnose_candidate(candidate) == []


class SequenceTransport(ScriptedTransport):
    def __init__(self, replies: list[dict[str, Any] | BaseException]) -> None:
        super().__init__('')
        self.replies = replies

    async def turn(self, messages: Any, tools: Any) -> Reply:
        self.requests.append({'messages': deepcopy(messages), 'tools': tools})
        candidate = self.replies.pop(0)
        if isinstance(candidate, BaseException):
            raise candidate
        return Reply([{'type': 'text', 'text': json.dumps(candidate)}], [], Usage(input=1, output=1), 'end_turn')


def review_request(check: Any, *, repairs: int = 1) -> Any:
    async def diagnose(candidate: Any) -> list[Any]:
        return diagnose_candidate(candidate)
    return replace(request(check), diagnose=diagnose, budget=AttemptBudget(max_calls=8, max_schema_repairs=repairs),
        answer_schema={'type': 'object', 'properties': {'scope_assessment': {'type': 'object',
            'properties': {'rationale': {'type': 'string', 'maxLength': 2048},
                'sources': {'type': 'array', 'items': {'type': 'string'}}},
            'required': ['rationale', 'sources']}}, 'required': ['scope_assessment']})


def candidate(rationale: str = 'Verified source', source: str = 'README.md') -> dict[str, Any]:
    return {'scope_assessment': {'rationale': rationale, 'sources': [source]}}


@pytest.mark.asyncio
async def test_hidden_schema_and_citation_errors_share_one_native_correction() -> None:
    admitted = []
    async def check(answer: Any) -> list[Any]:
        admitted.append(deepcopy(answer))
        return []
    good = candidate()
    transport = SequenceTransport([candidate('x' * 2266, 'unread.md'), good])
    req = review_request(check)
    result = await NareRuntime(transport=transport).run(req)
    assert result.status == 'accepted' and result.answer == good
    assert admitted == [good]
    feedback = str(transport.requests[1]['messages'])
    assert '2266' in feedback and 'scope_citations_invalid' in feedback
    assert req.budget.schema_repairs == 1 and req.budget.calls == 2


@pytest.mark.asyncio
async def test_diagnostic_cancellation_does_not_admit_or_spend_correction() -> None:
    async def check(answer: Any) -> list[Any]:
        pytest.fail('Admission must not run after cancelled diagnostics')
    async def cancel(answer: Any) -> list[Any]:
        raise asyncio.CancelledError
    req = replace(review_request(check), diagnose=cancel)
    result = await NareRuntime(transport=SequenceTransport([candidate()])).run(req)
    assert result.status == 'cancelled' and result.answer is None
    assert req.budget.schema_repairs == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('shard_corrections', [None, 1])
async def test_diagnostic_correction_survives_cancellation_and_resume(tmp_path: Any, shard_corrections: int | None) -> None:
    async def check(answer: Any) -> list[Any]:
        return []
    req = replace(review_request(check), checkpoint_store=CheckpointStore(tmp_path), shard_corrections=shard_corrections)
    runtime = NareRuntime(transport=SequenceTransport([candidate(source='unread.md'), asyncio.CancelledError()]))
    first = await runtime.run(req)
    counter = 'shard_schema_repairs' if shard_corrections is not None else 'schema_repairs'
    assert first.status == 'cancelled' and getattr(req.budget, counter) == 1
    resumed = await NareRuntime(transport=SequenceTransport([candidate(source='unread.md')])).run(
        replace(req, checkpoint_key=first.checkpoint_key))
    assert resumed.status == 'held' and resumed.hold_code == 'attempt_schema_limit'
    assert getattr(req.budget, counter) == 1 and resumed.answer is None


def test_executable_assessment_does_not_invent_a_top_level_summary_bound() -> None:
    assert diagnose_candidate({'assessment': {'rationale': 'é' * 3000, 'sources': ['README.md']}}, 'assessment') == []


def test_mixed_shape_sources_still_report_independent_unread_citations() -> None:
    issues = diagnose_candidate({'scope_assessment': {'sources': [None, 'unread.md']}})
    assert len(issues) == 1 and issues[0].code == 'scope_citations_invalid'
    assert diagnostic_details(issues[0].message)['invalid_source_indices'] == [1]


@pytest.mark.asyncio
async def test_phase_diagnostics_are_pure_and_policy_is_checkpoint_bound(monkeypatch: Any) -> None:
    import coordinare.services.environment_agent as adapter
    from coordinare.services.agent_runtime.evidence import EvidenceRead
    from coordinare.services.agent_runtime.models import AgentResult
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_inspection import SourceInspection

    validations = []
    identities = []
    async def fetch(path: str) -> str:
        return 'Fictional project setup'
    sources = adapter.PinnedSourceTools(SourceInspection(['README.md'], fetch, DiscoveryLimits()))
    await sources.read({'path': 'README.md'})

    def validate(answer: Any) -> Any:
        validations.append(deepcopy(answer))
        return answer

    class Runtime:
        async def run(self, req: Any) -> AgentResult:
            identities.append(req.identity)
            assert req.diagnose is not None
            before = len(validations)
            assert await req.diagnose(candidate('x' * 2266, 'unread.md'))
            assert len(validations) == before
            while req.evidence_catalog.remaining_required:
                await req.evidence_catalog.read(EvidenceRead())
            assert await req.check(candidate()) == []
            return AgentResult(status='accepted', answer=candidate())

    phase = adapter.EnvironmentPhaseRequest(attempt_id='review-fixture', phase='scope_review', role='reviewer',
        source_commit='a' * 40, domain_digests=(), model_config_digest='b' * 64, instructions='Review the source.',
        packet={}, sources=(), required=frozenset(), envelope='scope_assessment', schema={'type': 'object'},
        validate=validate, runtime=Runtime(), budget=AttemptBudget(), phase_timeout_seconds=10, source_tools=sources)
    first = await adapter.run_environment_phase(phase)
    monkeypatch.setattr(adapter, 'REVIEW_DIAGNOSTICS_POLICY', 'review_sources_and_utf8_v2')
    second = await adapter.run_environment_phase(phase)
    assert first.data == second.data == candidate()['scope_assessment']
    assert len(validations) == 2
    assert identities[0].policy_digest != identities[1].policy_digest
    assert identities[0].schema_digest == identities[1].schema_digest


def test_invalid_unicode_is_diagnostic_without_rewriting_candidate() -> None:
    value = candidate('\ud800')
    before = deepcopy(value)
    issues = diagnose_candidate(value)
    assert [issue.code for issue in issues] == ['response_utf8_invalid']
    assert value == before


@pytest.mark.asyncio
@pytest.mark.parametrize('complete_reads', [True, False])
async def test_phase_reports_all_unread_evidence_with_first_schema_correction(complete_reads: bool) -> None:
    from nare import ToolCall

    from coordinare.services.environment_agent import (
        EnvironmentPhaseRequest,
        ObservationTools,
        PinnedSourceTools,
        run_environment_phase,
    )
    from coordinare.services.environment_attempt import EnvironmentDiscoveryError
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_inspection import SourceInspection

    async def fetch(path: str) -> str:
        return 'Pinned fictional setup evidence. ' * 3000
    sources = PinnedSourceTools(SourceInspection(['README.md'], fetch, DiscoveryLimits()))
    await sources.read({'path': 'README.md'})
    observations = ObservationTools()
    observation = json.loads(await observations.add({'result': 'Fictional runtime observation. ' * 3000}))
    observation_id = observation['observation_id']
    assert sources.issues() and observations.issues()
    source_receipt = sources.catalogs['README.md'].receipt()
    observation_receipt = observations.catalogs[observation_id].receipt()
    admitted = []
    first_feedback = []
    diagnostic_receipts = []

    class CompletingTransport(ScriptedTransport):
        required_tool_choice = 'required'
        evidence: Any
        initial_evidence_receipt: Any

        async def turn(self, messages: Any, tools: Any, *, tool_choice: str | None = None) -> Reply:
            self.requests.append({'messages': deepcopy(messages), 'tools': tools})
            if len(self.requests) == 1:
                return Reply([{'type': 'text', 'text': json.dumps(candidate('x' * 2266))}],
                    [], Usage(input=1, output=1), 'end_turn')
            if len(self.requests) == 2:
                first_feedback.append(str(messages[-1].content))
                diagnostic_receipts.append((sources.catalogs['README.md'].receipt(),
                    observations.catalogs[observation_id].receipt(), self.evidence.receipt(), list(admitted)))
            if not complete_reads:
                return Reply([{'type': 'text', 'text': json.dumps(candidate())}],
                    [], Usage(input=1, output=1), 'end_turn')
            if tool_choice == 'required':
                call = ToolCall(str(len(self.requests)), 'continue_required_reads', {})
            elif sources.issues():
                call = ToolCall(str(len(self.requests)), 'read_source', {'path': 'README.md'})
            elif observations.issues():
                call = ToolCall(str(len(self.requests)), 'read_observation', {'observation_id': observation_id})
            elif self.evidence.remaining_required:
                call = ToolCall(str(len(self.requests)), 'review_evidence', {})
            else:
                return Reply([{'type': 'text', 'text': json.dumps(candidate())}],
                    [], Usage(input=1, output=1), 'end_turn')
            assert call.name in {tool['name'] for tool in tools}
            return Reply([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': call.args}],
                [call], Usage(input=1, output=1), 'tool_use')

    transport = CompletingTransport('')

    class Runtime:
        async def run(self, req: Any) -> Any:
            transport.evidence = req.evidence_catalog
            transport.initial_evidence_receipt = req.evidence_catalog.receipt()
            return await NareRuntime(transport=transport).run(req)

    def validate(answer: Any) -> Any:
        admitted.append(deepcopy(answer))
        return answer

    budget = AttemptBudget(max_calls=16, max_schema_repairs=1)
    phase = EnvironmentPhaseRequest(attempt_id='read-completion', phase='scope_review', role='reviewer',
        source_commit='a' * 40, domain_digests=(), model_config_digest='b' * 64, instructions='Review the source.',
        packet={'context': 'Immutable fictional phase context. ' * 3000}, sources=(), required=frozenset(),
        envelope='scope_assessment', schema=review_request(None).answer_schema,
        validate=validate, runtime=Runtime(), budget=budget, phase_timeout_seconds=10,
        source_tools=sources, observations=observations)
    if complete_reads:
        result = await run_environment_phase(phase)
        assert result.agent.status == 'accepted' and result.data == candidate()['scope_assessment']
        assert not result.evidence.remaining_required
    else:
        with pytest.raises(EnvironmentDiscoveryError) as held:
            await run_environment_phase(phase)
        assert held.value.code == 'attempt_schema_limit'
        assert sources.issues() and observations.issues() and transport.evidence.remaining_required
        assert not admitted
    assert len(first_feedback) == 1 and '2266' in first_feedback[0]
    assert all(name in first_feedback[0] for name in ('read_source', 'read_observation', 'review_evidence'))
    assert diagnostic_receipts == [(source_receipt, observation_receipt, transport.initial_evidence_receipt, [])]
    if complete_reads:
        assert admitted == [candidate()['scope_assessment']]
        assert not sources.issues() and not observations.issues()
    assert budget.schema_repairs == budget.max_schema_repairs == 1
    assert budget.calls == budget.calls_completed == len(transport.requests)


def test_a_text_bound_correction_names_how_much_to_cut_and_a_target_with_margin() -> None:
    """nare-native-004: a shard reviewer trimmed 22-84 bytes per correction (2252..2052) until its allowance ran out."""
    from coordinare.services.environment_response_diagnostics import (
        REVIEW_TEXT_MAX_UTF8_BYTES,
        REVIEW_TEXT_TARGET_UTF8_BYTES,
    )

    issues = diagnose_candidate({'scope_assessment': {'rationale': 'ok', 'sources': ['README.md'],
                                                      'findings': [{'issue': 'a' * 2252, 'sources': ['README.md']}],
                                                      'decisions': []}})
    [issue] = [item for item in issues if item.code == 'response_utf8_limit']
    details = diagnostic_details(issue.message)
    assert details['remove_at_least_utf8_bytes'] == 2252 - REVIEW_TEXT_MAX_UTF8_BYTES
    assert details['target_utf8_bytes'] == REVIEW_TEXT_TARGET_UTF8_BYTES < REVIEW_TEXT_MAX_UTF8_BYTES
    assert 'at least 204 UTF-8 bytes' in details['guidance'] and str(REVIEW_TEXT_TARGET_UTF8_BYTES) in details['guidance']
    assert details['max_utf8_bytes'] == 2048


def test_a_bound_still_broken_keeps_one_identity_and_names_where_to_cut() -> None:
    """nare-native-009: 2161, 2089, 2083 and 2070 bytes read to nare as four different checks, and each correction
    told the reviewer three of them were fixed. The rule now precedes nare's ", got " separator."""
    from nare.corrections import code_of

    from coordinare.services.environment_response_diagnostics import text_bound

    _, target = text_bound('rationale')
    def issue(text: str) -> Any:
        found = diagnose_candidate({'scope_assessment': {'rationale': text, 'sources': ['README.md'],
                                                         'findings': [], 'decisions': []}})
        [only] = [item for item in found if item.code == 'response_utf8_limit']
        return only

    head = 'k' * target
    first, second = issue(head + ' Marker passage starts here. ' + 'z' * 900), issue(head + ' Marker passage ' + 'y' * 850)
    rendered = [f"scope_assessment.rationale: {item.message}" for item in (first, second)]
    assert code_of(rendered[0]) == code_of(rendered[1]) == 'scope_assessment.rationale: exceeds 4096 UTF-8 bytes'
    details = diagnostic_details(first.message)
    assert details['passage_past_target'].startswith('Marker passage starts here.')
    assert 'end the summary before it' in details['guidance']
    # A multi-byte character straddling the target is never split.
    assert diagnostic_details(issue('é' * 2100).message)['passage_past_target'].startswith('é')


def test_reviewer_rationales_take_4096_bytes_while_issues_and_authored_text_keep_2048() -> None:
    """User ruling after nare-native-010: review summaries were lost at 2048 bytes three times."""
    from coordinare_service_inference.environment.findings import (
        FindingDecisionProposal,
        FindingResponse,
    )
    from coordinare_service_inference.environment.models import CriterionRulingProposal
    from coordinare_service_inference.environment.scope import (
        CoveredDecision,
        ScopeAssessmentProposal,
        ScopeCriterionProposal,
        bounded_rationale,
    )
    from pydantic import ValidationError

    long, too_long = 'r' * 4096, 'r' * 4097
    ScopeAssessmentProposal(verdict='accept', rationale=long, sources=['a'], findings=[], decisions=[])
    FindingDecisionProposal(finding_id='F0001', status='resolved', rationale=long, sources=['a'])
    CoveredDecision(finding_id='F0001', rationale=long, sources=['a'], disposition='covered', criterion_ids=['S1'])
    CriterionRulingProposal(criterion_id='S1', verdict='accept', rationale=long)
    for build in (lambda: ScopeAssessmentProposal(verdict='accept', rationale=too_long, sources=['a'], findings=[],
                                                  decisions=[]),
                  lambda: FindingDecisionProposal(finding_id='F0001', status='resolved', rationale=too_long,
                                                  sources=['a'])):
        with pytest.raises(ValidationError):
            build()
    with pytest.raises(ValidationError):
        FindingResponse(finding_id='F0001', rationale='r' * 2049, sources=['a'], affected_ids=[])
    with pytest.raises(ValidationError):
        ScopeCriterionProposal(description='d', sources=['a'], applicability='required', rationale='r' * 2049,
                               usability='u', runtime_needs=[])
    assert bounded_rationale(long) == long and len(bounded_rationale(too_long).encode()) == 4096
