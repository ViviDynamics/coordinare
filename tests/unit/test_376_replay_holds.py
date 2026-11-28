"""Recorded inputs from four held acceptance runs, replayed offline through the current code (issue 376).

Each fixture under tests/fixtures/replay/ was taken from the run's own checkpoints, transcripts or journal by
scripts/replay_extract.py and redacted (record ids kept; commands, paths and prose replaced consistently). Each test
first checks what the recording shows happened, then feeds the same input to the current harness and asserts the
handling the fix intended. nare-native-041's volume was not mounted when the corpus was built, so its fixture is a
synthetic stand-in with the recorded structure (see its ``synthetic_reason``).
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from coordinare_service_inference.environment.findings import FindingLedger
from coordinare_service_inference.environment.models import (
    Command,
    Service,
    ShardChecks,
    validate_execution_graph,
    validate_unique_content,
)
from coordinare_service_inference.environment.scope import (
    ScopeAssessmentEnvelope,
    normalize_source_assessment,
)
from nare import Reply, ToolCall, Usage
from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from coordinare.services import environment_discovery as discovery
from coordinare.services.agent_runtime.evidence_protocol import MAX_INVALID_EVIDENCE_CALLS
from coordinare.services.environment_agent import run_environment_phase
from coordinare.services.environment_attempt import (
    EVIDENCE_PROTOCOL,
    AttemptBudget,
    EnvironmentDiscoveryError,
)
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_inspection import SourceInspection
from coordinare.services.environment_scope import _validate_review
from coordinare.services.environment_shard_construction import (
    CheckGroup,
    DividedConstruction,
    FoldPolicy,
    merge_parts,
)
from coordinare.services.environment_shard_scope import combine_round
from tests.replay.fixtures import ReplayFixture, load_fixture
from tests.replay.transport import ReplayTransport
from tests.unit.test_353_review_findings_flow import review
from tests.unit.test_353_scope_contract import COMMIT, Replies, criterion, proposal, run
from tests.unit.test_353_scope_contract import review as scope_review
from tests.unit.test_353_sharded_executable import FakeBuild
from tests.unit.test_376_environment_review import phase

GROUPS = ('requirements', 'install', 'checks', 'services')


# ----- nare-native-046: a reply of 20 invalid parallel calls -----

class FinishReview:
    """What the session does after the recording: finish the required reads, read the rest, then answer."""

    async def turn(self, messages: Any, tools: Any, *, tool_choice: str | None = None) -> Reply:
        unread = 1
        for message in messages:
            for block in message.content:
                if block.get('type') == 'tool_result' and not block.get('is_error'):
                    unread = json.loads(block['content'])['unread_count']
        if tool_choice == 'required' or unread:
            name = 'continue_required_reads' if tool_choice == 'required' else 'review_evidence'
            call = ToolCall(f'finish-{len(messages)}', name, {})
            return Reply([{'type': 'tool_use', 'id': call.id, 'name': name, 'input': {}}], [call],
                         Usage(input=10, output=5), 'tool_use')
        return Reply([{'type': 'text', 'text': json.dumps(review())}], [], Usage(input=10, output=5), 'end_turn')


@pytest.mark.asyncio
async def test_046_a_recorded_reply_of_twenty_invalid_calls_costs_one_strike(tmp_path: Any) -> None:
    """The recorded first reply of a shard author: 20 parallel continue_required_reads calls with an invented
    argument, sent while that tool was the only one offered and tool_choice was required. The run charged all 20
    against the session's allowance of 8 and held before the model saw a refusal. Replayed into a session with the
    same first-turn protocol, the reply is one strike, and the next request carries all 20 refusals."""
    fixture = load_fixture('nare-native-046', 'first-reply-batch')
    (step,) = fixture.steps
    calls = step.reply['tool_calls']
    assert len(calls) == 20 and {call['name'] for call in calls} == {'continue_required_reads'}
    assert all(call['args'] for call in calls)  # Each call invented an argument; the tool takes none.
    assert step.recorded['refusal_codes'] == ['invalid_tool_arguments'] * 20
    assert step.recorded['evidence_strikes'] == 20 > MAX_INVALID_EVIDENCE_CALLS
    assert fixture.hold['code'] == 'agent_evidence_call_limit'

    transport = ReplayTransport(fixture.steps, then=FinishReview())
    request = replace(phase(tmp_path, transport), budget=AttemptBudget(max_calls=40))
    assert request.budget.agent_evidence_protocol == EVIDENCE_PROTOCOL == 2
    result = await run_environment_phase(request)
    transport.assert_followed()
    assert result.agent.status == 'accepted'  # No hold: the session goes on to answer.
    assert request.budget.agent_evidence_errors == 20  # Every invalid call is still recorded...
    assert list(request.budget.agent_evidence_phase_errors.values()) == [1]  # ...as one strike for the reply.
    refusals = [block for message in transport.requests[1] for block in message.content
                if block.get('type') == 'tool_result']
    assert len(refusals) == 20
    assert all(block['is_error'] and 'takes no parameters' in block['content'] for block in refusals)


# ----- nare-native-045: one obligation per correction -----

class Admitted(ValueError):  # noqa: N818 - a signal that admission was reached, not an error
    """The candidate passed obligation preservation; the rest of admission is not part of this replay."""

    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__('obligations preserved')
        self.data = data


def _admit(data: dict[str, Any], _inputs: Any) -> Any:
    raise Admitted(copy.deepcopy(data))


def _validator(prior: dict[str, Any]) -> tuple[discovery._CandidateValidator, AttemptBudget]:
    """The full repair's validator as _initial_repair builds it: required ids and the ledger from the prior manifest.

    Admission past obligation preservation (construction.admit and its static rules) needs the run's scope contract
    and sources, which the fixture does not carry; it is replaced by ``Admitted``.
    """
    ledger = discovery._ObligationLedger()
    ledger.record(copy.deepcopy(prior))
    required = {group: {item['id'] for item in prior.get(group) or [] if item.get('required', True)} for group in GROUPS}
    construction = SimpleNamespace(budget=SimpleNamespace(scope_contract=None), admit=_admit)
    budget = AttemptBudget(max_calls=8)
    return discovery._CandidateValidator(SimpleNamespace(inputs={}), construction, required, [], ledger, budget), budget


def _named(message: str, known: set[str]) -> set[str]:
    return {word for word in re.findall(r'[A-Za-z0-9_.:-]+', message) if word in known}


def _known_ids(fixture: ReplayFixture) -> set[str]:
    prior = fixture.state['prior_manifest']
    known = {item['id'] for group in GROUPS for item in prior.get(group) or []}
    for step in fixture.steps:
        manifest = step.reply['candidate']['manifest']
        known |= {item['id'] for group in GROUPS for item in manifest.get(group) or []}
    return known


def test_045_one_refusal_names_every_obligation_the_recorded_corrections_named_one_at_a_time() -> None:
    """The full-repair author's reply 495 had lost three requirement-to-check links and one requirement id. The run
    refused it, then its next three corrections, one loss each (calls 495-498). Replayed against the ledger loaded
    from the prior manifest, reply 495 alone gets one obligation_removed refusal naming all of them."""
    fixture = load_fixture('nare-native-045', 'one-obligation-refusal')
    one_at_a_time = fixture.steps[:4]
    assert [step.recorded['refusal_codes'] for step in one_at_a_time] == [['obligation_removed']] * 4
    recorded = set().union(*(step.recorded['guidance_ids'] for step in one_at_a_time))
    assert recorded == {'req-python-env', 'c-bench-evidence', 'req-pytest-runner', 'c-docker-run', 'c-sandbox',
                        'req-offline-suite', 'req-pytest-offline'}

    validator, budget = _validator(fixture.state['prior_manifest'])
    with pytest.raises(EnvironmentDiscoveryError) as refused:
        validator(copy.deepcopy(one_at_a_time[0].reply['candidate']['manifest']))
    assert refused.value.code == 'obligation_removed'
    assert _named(str(refused.value), _known_ids(fixture)) == recorded
    # A refused reply retains nothing, so the corrected reply is judged against the prior manifest alone.
    assert validator.obligation_ledger.coverage == _validator(fixture.state['prior_manifest'])[0].obligation_ledger.coverage
    assert budget.validation_codes == ['install_dependency_stripped']


def test_045_install_dependencies_are_stripped_rather_than_refused() -> None:
    """Reply 499 made every check depend on install ids and was refused clean_dependency_install_only (and its
    regression at 559 was the seventeenth correction that held the attempt). The current validator strips those
    dependencies itself: the reply passes obligation preservation and the execution graph accepts it."""
    fixture = load_fixture('nare-native-045', 'one-obligation-refusal')
    step = fixture.steps[4]
    assert step.recorded['refusal_codes'] == ['clean_dependency_install_only']
    manifest = step.reply['candidate']['manifest']
    install_ids = {item['id'] for item in manifest['install']}
    offenders = sorted(set(step.recorded['guidance_ids']) - install_ids)
    with pytest.raises(PydanticCustomError) as recorded_refusal:
        _graph(manifest)
    assert recorded_refusal.value.type == 'clean_dependency_install_only'
    assert sorted(recorded_refusal.value.context['command_ids']) == offenders  # The recording, reproduced.

    validator, budget = _validator(fixture.state['prior_manifest'])
    with pytest.raises(Admitted) as admitted:
        validator(copy.deepcopy(manifest))
    assert budget.validation_codes == ['install_dependency_stripped']
    stripped = admitted.value.data
    assert not any(install_ids & set(check.get('depends_on') or []) for check in stripped['checks'])
    _graph(stripped)
    assert sorted(discovery.strip_install_dependencies(copy.deepcopy(manifest))) == offenders


def _graph(manifest: dict[str, Any]) -> None:
    validate_execution_graph([Command.model_validate(item) for item in manifest['install']],
                             [Command.model_validate(item) for item in manifest['checks']],
                             [Service.model_validate(item) for item in manifest.get('services') or []])


# ----- nare-native-038: a pinned group check duplicated by the global author -----

def _merge_inputs(fixture: ReplayFixture) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[CheckGroup]]:
    authored = {step.session: step.reply['candidate'] for step in fixture.steps}
    prefixes = fixture.state['merge']['groups']
    parts = {prefix: authored[prefix]['shard_checks'] for prefix in prefixes}
    return authored[fixture.state['merge']['global']], parts, [CheckGroup(prefix, ()) for prefix in prefixes]


def _unique(merged: dict[str, Any]) -> dict[str, Any]:
    """The admission's duplicate-content rule, on its own (the merge's other checks need the run's scope)."""
    try:
        validate_unique_content([Command.model_validate(check) for check in merged['checks']], checks=True)
    except PydanticCustomError as exc:
        raise ValidationError.from_exception_data('EnvironmentManifestProposal',
                                                  [{'type': exc, 'loc': (), 'input': None}]) from exc
    return merged


def test_038_the_recorded_duplicate_of_a_kept_group_check_goes_to_the_global_author() -> None:
    fixture = load_fixture('nare-native-038', 'pinned-pair')
    global_part, parts, groups = _merge_inputs(fixture)
    assert fixture.state['recorded_merge_codes'][-4:] == ['duplicate_command_content'] * 4
    pinned = DividedConstruction.pinned_checks(SimpleNamespace(groups=groups, previous=parts), groups)
    assert 'c00-replay-done-jsonl' in pinned  # c00 was told to keep it, so c00 can neither drop nor rename it.

    shards = {group.prefix: (group, ShardChecks.model_validate(parts[group.prefix])) for group in groups}
    merged = merge_parts(global_part, [part for _group, part in shards.values()], policy=FoldPolicy(), folded={})
    with pytest.raises(ValidationError) as rejected:
        _unique(merged)
    errors = discovery._validation_feedback(rejected.value)
    assert [error['pairs'] for error in errors] == [[['check-replay-done', 'c00-replay-done-jsonl']]]
    # As recorded: judged by executed checks only, the pair went to c00, which could not fix it.
    unpinned = discovery._group_errors(copy.deepcopy(errors), frozenset())
    assert [group.prefix for group in discovery._attributed(unpinned, rejected.value, shards)] == ['c00-']
    # Now: the pinned pair names no group, so the global author corrects it, told to drop its copy.
    judged = discovery._group_errors(errors, pinned)
    assert judged == []
    assert discovery._attributed(judged, rejected.value, shards) == []
    assert discovery.GLOBAL_COPY_GUIDANCE in errors[0]['guidance']
    assert discovery._global_copies(rejected.value, pinned) == {'check-replay-done'}


class ReplayBuild(FakeBuild):
    """Every round returns the recorded group parts; what each group was told to keep is the recorded part."""

    def __init__(self, groups: list[CheckGroup], parts: dict[str, dict[str, Any]]) -> None:
        super().__init__(groups)
        self.recorded = parts
        self.previous = copy.deepcopy(parts)

    def pinned_checks(self, groups: Any) -> frozenset[str]:
        return DividedConstruction.pinned_checks(self, groups)  # type: ignore[arg-type]

    async def author_all(self, *, summary: Any, probes: Any, groups: Any = None, concurrency: Any = None) -> Any:
        asked = list(self.groups if groups is None else groups)
        self.rounds.append([group.prefix for group in asked])
        return {group.prefix: (group, ShardChecks.model_validate(copy.deepcopy(self.recorded[group.prefix])))
                for group in asked}


@pytest.mark.asyncio
async def test_038_a_merge_that_cannot_converge_holds_with_a_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recording held executable_merge_invalid with error_detail None after three identical rounds. Replayed with
    the global author and the groups returning their recorded parts, the pair goes to the global author, and when
    its unchanged reply repeats the errors the merge ends early with a detail naming the pair."""
    fixture = load_fixture('nare-native-038', 'pinned-pair')
    global_part, parts, groups = _merge_inputs(fixture)
    assert fixture.hold['code'] == 'executable_merge_invalid'
    build = ReplayBuild(groups, parts)
    phases: list[int] = []

    async def global_author(**_kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        phases.append(1)
        return copy.deepcopy(global_part), {}

    async def observe(_inputs: Any) -> None:
        return None

    monkeypatch.setattr(discovery, '_phase', global_author)
    budget = AttemptBudget(max_calls=1024, max_reads=8192, max_coverage_revisions=8, max_schema_repairs=8)
    messages = [{'role': 'system', 'content': ''}, {'role': 'user', 'content': '{}'}]
    attempt = SimpleNamespace(budget=budget, required_ids={}, obligation_ledger=discovery._ObligationLedger(),
                              discovery=SimpleNamespace(budget=budget, inputs={}, finding_responses=[]),
                              messages=messages, chat=None, delta=None, prior_manifests=[],
                              construction=SimpleNamespace(observe_sources=observe))
    with pytest.raises(EnvironmentDiscoveryError) as held:
        await discovery._divided_candidate(build, attempt, _unique)  # type: ignore[arg-type]
    assert held.value.code == 'executable_merge_invalid'
    assert held.value.detail is not None
    assert 'duplicate_command_content' in held.value.detail
    assert 'check-replay-done' in held.value.detail and 'c00-replay-done-jsonl' in held.value.detail
    assert phases == [1, 1]  # The global author was sent the pair; no group round was spent on it.
    assert discovery.GLOBAL_COPY_GUIDANCE in messages[-1]['content']


# ----- nare-native-041: two shard reviewers file the same finding -----

@pytest.mark.asyncio
async def test_041_duplicate_shard_findings_merge_into_one_finding_with_both_requests_bound() -> None:
    """Synthetic (see the fixture): the run held scope_investigation_identity when two shard reviews' identical
    remote_state findings merged in the ledger but not in the inspection bindings. Combined as a shard round is,
    they are one finding, and both inspection requests bind to its one id."""
    fixture = load_fixture('nare-native-041', 'duplicate-shard-findings')
    assert fixture.synthetic and fixture.hold['code'] == 'scope_investigation_identity'
    recorded_criterion = fixture.state['criterion_ids'][0]
    sources = {path: f'Synthetic source {path} for a remote state obligation.'
               for path in [*fixture.state['read'], *fixture.state['unread']]}

    async def fetch(path: str) -> str | None:
        return sources.get(path)

    first = fixture.state['read'][0]
    scope = await run(Replies([{'tool': 'read_file', 'path': first}, proposal(criterion(first)),
                               scope_review(sources=[first])]), fetch=fetch, paths=list(sources))
    criterion_id = scope.definition.criteria[0].id
    budget = AttemptBudget(scope_findings=FindingLedger(source_commit=COMMIT))
    reviewer = SourceInspection(list(sources), fetch, DiscoveryLimits(), budget)
    await reviewer.read_files(fixture.state['read'])
    parts = []
    for step in fixture.steps:
        # The recorded criterion id is replaced by this scope's: both findings name the same one, as recorded.
        wire = json.loads(json.dumps(step.reply['candidate']).replace(recorded_criterion, criterion_id))
        parts.append((step.session, ScopeAssessmentEnvelope.model_validate(
            normalize_source_assessment(wire)).scope_assessment, {}))
    combined = combine_round(parts, ledger=budget.scope_findings)
    assert len(combined['scope_assessment']['findings']) == 2
    assessment, ledger, record = _validate_review(combined, reviewer=reviewer, definition=scope.definition,
                                                  ledger=budget.scope_findings, commit=COMMIT)
    assert len(assessment.findings) == 1 and len(record.assessment.findings) == 1
    assert ledger.reviews[-1].added_ids == ['F0001']
    assert {binding.finding_id for binding in budget.scope_investigations} == {'F0001'}
    assert sorted(binding.request.path for binding in budget.scope_investigations) == ['src/f02', 'src/f03']
