"""Divided executable construction: a global author, prefixed check authors, a deterministic merge."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment.models import CoverageAssessmentProposal, ShardChecks
from coordinare_service_inference.environment.scope import (
    ScopeCriterion,
    ScopeCriterionProposal,
    criterion_id,
)
from pydantic import ValidationError

from coordinare.services import environment_discovery as discovery_module
from coordinare.services.environment_admission import _validation_feedback
from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_scope import _ResponseReferenceError
from coordinare.services.environment_shard_construction import (
    GLOBAL_OWNER,
    CheckGroup,
    check_groups,
    combine_review,
    divided,
    finding_part,
    merge_parts,
    validate_global,
    validate_group,
)


def make_criterion(index: int, *, applicability: str = 'required') -> ScopeCriterion:
    proposal = ScopeCriterionProposal.model_validate({
        'description': f'Obligation {index}', 'sources': [f'src/{index:03d}.md'], 'applicability': applicability,
        'rationale': 'Cited source obligation', 'usability': f'{index} succeeds', 'runtime_needs': []})
    return ScopeCriterion.model_validate({**proposal.model_dump(), 'id': criterion_id(proposal)})


def scope_of(criteria: list[ScopeCriterion]) -> SimpleNamespace:
    return SimpleNamespace(definition=SimpleNamespace(criteria=criteria))


def global_part(**changes: object) -> dict:
    data = {'requirements': [], 'install': [], 'activation': {}, 'services': [], 'local_credentials': [],
            'checks': [{'id': 'toolchain', 'argv': ['true']}], 'criterion_checks': {}, 'runtime_endpoints': [],
            'empty_reason': None}
    return {**data, **changes}


SUMMARY = {'checks': [{'id': 'toolchain', 'required': True, 'scope': []},
                      {'id': 'scoped', 'required': True, 'scope': ['ci']}],
           'readiness_ids': ['db-start']}


def group_reply(group: CheckGroup, **changes: object) -> dict:
    checks = [{'id': f'{group.prefix}{index}', 'argv': ['true']} for index in range(len(group.criteria))]
    mapping = {criterion.id: [f'{group.prefix}{index}'] for index, criterion in enumerate(group.criteria)}
    return {'shard_checks': {'checks': checks, 'criterion_checks': mapping, **changes}}


def test_only_a_scope_beyond_one_authors_share_is_divided_into_groups_of_25():
    assert not divided(scope_of([make_criterion(index) for index in range(40)]))
    criteria = [make_criterion(index) for index in range(60)] + [make_criterion(99, applicability='not_applicable')]
    scope = scope_of(criteria)
    assert divided(scope)
    groups = check_groups(scope)
    assert [group.prefix for group in groups] == ['c00-', 'c01-', 'c02-']
    assert [len(group.criteria) for group in groups] == [25, 25, 10]
    assert all(criterion.applicability == 'required' for group in groups for criterion in group.criteria)


@pytest.mark.parametrize('changes', [{'criterion_checks': {'S0000000000000000': ['toolchain']}},
                                     {'checks': [{'id': 'c00-mine', 'argv': ['true']}]}])
def test_the_global_author_cannot_map_criteria_or_take_a_check_author_prefix(changes):
    with pytest.raises(ValidationError) as caught:
        validate_global(global_part(**changes))
    [feedback] = _validation_feedback(caught.value)
    assert feedback['code'] == 'global_part_invalid'
    assert 'check authors' in feedback['guidance']


def test_a_group_part_is_accepted_with_its_prefix_and_its_whole_mapping():
    group = CheckGroup('c00-', tuple(make_criterion(index) for index in range(3)))
    part = validate_group(group_reply(group), group=group, summary=SUMMARY)
    assert {check.id for check in part.checks} == {'c00-0', 'c00-1', 'c00-2'}


@pytest.mark.parametrize(('mutate', 'code'), [
    (lambda reply: reply['shard_checks']['checks'][0].update(id='c01-0'), 'check_prefix_invalid'),
    (lambda reply: reply['shard_checks']['checks'][0].update(depends_on=['install-everything']), 'check_dependency_unknown'),
    (lambda reply: reply['shard_checks']['criterion_checks'].popitem(), 'criterion_mapping_incomplete'),
    (lambda reply: reply['shard_checks']['criterion_checks'].update(
        {next(iter(reply['shard_checks']['criterion_checks'])): ['scoped']}), 'criterion_mapping_invalid'),
])
def test_a_group_part_outside_its_share_is_a_correctable_rejection(mutate, code):
    group = CheckGroup('c00-', tuple(make_criterion(index) for index in range(3)))
    reply = group_reply(group)
    mutate(reply)
    with pytest.raises(_ResponseReferenceError) as caught:
        validate_group(reply, group=group, summary=SUMMARY)
    assert code in {error['code'] for error in caught.value.errors}


def test_the_merge_unions_parts_and_refuses_to_choose_between_collisions():
    first = CheckGroup('c00-', (make_criterion(1),))
    second = CheckGroup('c01-', (make_criterion(2),))
    parts = [validate_group(group_reply(group), group=group, summary=SUMMARY) for group in (first, second)]
    merged = merge_parts(global_part(), parts)
    assert [check['id'] for check in merged['checks']] == ['toolchain', 'c00-0', 'c01-0']
    assert merged['criterion_checks'] == {make_criterion(1).id: ['c00-0'], make_criterion(2).id: ['c01-0']}
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        merge_parts(global_part(checks=[{'id': 'c00-0', 'argv': ['true']}]), parts)
    assert caught.value.code == 'executable_merge_collision'
    duplicate = ShardChecks.model_validate({'checks': [], 'criterion_checks': {make_criterion(1).id: ['toolchain']}})
    with pytest.raises(EnvironmentDiscoveryError):
        merge_parts(global_part(), [*parts, duplicate])


def test_findings_route_to_the_group_whose_checks_or_criteria_they_name():
    groups = [CheckGroup('c00-', (make_criterion(1),)), CheckGroup('c01-', (make_criterion(2),))]
    assert finding_part(['c01-0'], groups) == 'c01-'
    assert finding_part([make_criterion(1).id], groups) == 'c00-'
    assert finding_part(['install-node'], groups) == GLOBAL_OWNER


def assessment(verdict: str) -> CoverageAssessmentProposal:
    return CoverageAssessmentProposal.model_validate({'findings': [], 'decisions': [], 'capability_unavailable': False,
                                                      'verdict': verdict, 'rationale': 'judged', 'sources': ['a.md']})


def test_a_divided_review_accepts_only_when_every_part_accepts():
    ledger = SimpleNamespace(records=[])
    assert combine_review([('c00-', assessment('accept')), (GLOBAL_OWNER, assessment('accept'))], ledger)['verdict'] == 'accept'
    assert combine_review([('c00-', assessment('accept')), (GLOBAL_OWNER, assessment('revise'))], ledger)['verdict'] == 'revise'
    assert combine_review([('c00-', assessment('indeterminate')), (GLOBAL_OWNER, assessment('revise'))],
                          ledger)['verdict'] == 'indeterminate'


class FakeBuild:
    """Authors every group on demand, recording which groups each round asked for."""

    def __init__(self, groups: list[CheckGroup]) -> None:
        self.groups = groups
        self.feedback: dict = {}
        self.previous: dict = {}
        self.group_inputs: dict = {}
        self.rounds: list[list[str]] = []

    async def author_all(self, *, summary, probes, groups=None):
        asked = list(self.groups if groups is None else groups)
        self.rounds.append([group.prefix for group in asked])
        return {group.prefix: (group, validate_group(group_reply(group), group=group, summary=summary)) for group in asked}


async def merge_with(monkeypatch, validate):
    groups = [CheckGroup('c00-', (make_criterion(1),)), CheckGroup('c01-', (make_criterion(2),))]
    build = FakeBuild(groups)
    phases: list[int] = []

    async def phase(**_kwargs):
        phases.append(1)
        return global_part(), {}

    monkeypatch.setattr(discovery_module, '_phase', phase)
    budget = AttemptBudget(max_calls=1024, max_reads=8192, max_coverage_revisions=8)
    inspection = SimpleNamespace(budget=budget, inputs={}, finding_responses=[])

    async def observe(_inputs):
        return None

    messages = [{'role': 'system', 'content': ''}, {'role': 'user', 'content': '{}'}]
    attempt = SimpleNamespace(budget=budget, discovery=inspection, messages=messages, chat=None,
                              construction=SimpleNamespace(observe_sources=observe))
    merged = await discovery_module._divided_candidate(build, attempt, validate)
    return merged, build, phases, messages


@pytest.mark.asyncio
async def test_a_merge_error_naming_one_group_reauthors_only_that_group(monkeypatch):
    calls: list[int] = []
    criterion = make_criterion(2).id

    def validate(data):
        calls.append(1)
        if len(calls) == 1:
            message = f'criterion "{criterion}" needs a working check'
            raise EnvironmentDiscoveryError(message, code='obligation_removed')
        return data

    merged, build, phases, _messages = await merge_with(monkeypatch, validate)
    assert phases == [1]
    assert build.rounds == [['c00-', 'c01-'], ['c01-']]
    assert 'merge_errors' in build.feedback['c01-']
    assert 'c00-' not in build.feedback
    assert {check['id'] for check in merged['checks']} == {'toolchain', 'c00-0', 'c01-0'}


@pytest.mark.asyncio
async def test_a_merge_error_no_group_owns_goes_back_to_the_global_author(monkeypatch):
    calls: list[int] = []

    def validate(data):
        calls.append(1)
        if len(calls) == 1:
            message = 'repair must independently read every previous input'
            raise EnvironmentDiscoveryError(message, code='source_identity')
        return data

    _merged, build, phases, messages = await merge_with(monkeypatch, validate)
    assert phases == [1, 1]
    assert build.rounds == [['c00-', 'c01-'], ['c00-', 'c01-']]
    assert 'source_identity' in messages[-1]['content']


@pytest.mark.asyncio
async def test_a_merge_that_never_validates_ends_the_attempt(monkeypatch):
    def validate(_data):
        message = 'repair must independently read every previous input'
        raise EnvironmentDiscoveryError(message, code='source_identity')

    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await merge_with(monkeypatch, validate)
    assert caught.value.code == 'executable_merge_invalid'


@pytest.mark.asyncio
async def test_an_uncorrectable_merge_error_is_not_retried(monkeypatch):
    def validate(_data):
        message = 'private runtime endpoints require a configured platform policy'
        raise EnvironmentDiscoveryError(message, code='required_capability_unavailable')

    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await merge_with(monkeypatch, validate)
    assert caught.value.code == 'required_capability_unavailable'


class FakeRecord(SimpleNamespace):
    def model_dump(self, **_kwargs):
        return {'id': self.id, 'affected_ids': self.proposal.affected_ids}


class FakeManifest(SimpleNamespace):
    def digest(self):
        return 'd' * 64

    def model_dump(self, **_kwargs):
        return {'checks': self.checks, 'requirements': [], 'install': [], 'activation': {}, 'services': []}


@pytest.mark.asyncio
async def test_a_divided_review_gives_each_part_a_fresh_reviewer_and_only_its_own_findings(monkeypatch):
    first, second = make_criterion(1), make_criterion(2)
    groups = [CheckGroup('c00-', (first,)), CheckGroup('c01-', (second,))]
    records = [FakeRecord(id='F001', proposal=SimpleNamespace(affected_ids=['c01-0'])),
               FakeRecord(id='F002', proposal=SimpleNamespace(affected_ids=['install-node']))]
    budget = AttemptBudget(max_calls=1024, max_reads=8192, max_coverage_revisions=8)
    budget.finding_ledger = SimpleNamespace(records=records)
    seen: list[dict] = []

    class FreshInspection:
        def __init__(self, *_args):
            self.inputs: dict[str, str] = {}

        async def bind_verified(self, expected, **_kwargs):
            self.inputs.update(expected)
            return [{'path': path} for path in expected]

    async def agent(_context, inspection, messages, task):
        payload = json.loads(messages[1]['content'])
        seen.append({'owned': list(task.decision_ids), 'part': payload['part'], 'evidence': payload['mandatory_evidence'],
                     'history': 'untrusted_execution_history' in payload})
        verdict = 'revise' if task.decision_ids == ['F001'] else 'accept'
        return assessment(verdict), {**inspection.inputs, f'read-{len(seen)}.md': 'e' * 64}

    monkeypatch.setattr(discovery_module, '_Inspection', FreshInspection)
    monkeypatch.setattr(discovery_module, 'run_agent', agent)
    build = SimpleNamespace(groups=groups, scope=SimpleNamespace(definition=SimpleNamespace(criteria=[first, second])),
                            context=None, concurrency=4)
    manifest = FakeManifest(inputs={first.sources[0]: 'a' * 64, second.sources[0]: 'b' * 64, 'Makefile': 'c' * 64},
                            checks=[{'id': 'toolchain'}, {'id': 'c00-0'}, {'id': 'c01-0'}],
                            criterion_checks={first.id: ['c00-0'], second.id: ['c01-0']})
    attempt = SimpleNamespace(budget=budget, identity={'bound_scope_inputs': ['dropped'], 'symphony_name': 'suite'},
                              discovery=SimpleNamespace(finding_responses=[]))
    reviewer = SimpleNamespace(entries=None, paths=[], fetch=None, limits=None, inputs={'Makefile': 'c' * 64})
    combined, reads = await discovery_module._divided_review(build, attempt, manifest, reviewer)

    assert [entry['owned'] for entry in seen] == [[], ['F001'], ['F002']]
    assert [check['id'] for check in seen[0]['part']['checks']] == ['c00-0']
    assert [check['id'] for check in seen[2]['part']['checks']] == ['toolchain']
    assert [item['path'] for item in seen[0]['evidence']] == [first.sources[0]]
    assert len(seen[2]['evidence']) == 3
    assert [entry['history'] for entry in seen] == [False, False, True]
    assert combined['verdict'] == 'revise'
    assert set(reads) >= set(manifest.inputs)


class BindingInspection:
    """Records what a resumed author re-verified; a bound input is available to the merged manifest."""

    def __init__(self, *_args, **_kwargs):
        self.inputs: dict[str, str] = {}
        self.finding_responses: list = []
        self.name = ''
        self.bound: list[dict[str, str]] = []

    async def bind_verified(self, expected, **_kwargs):
        self.bound.append(dict(expected))
        self.inputs.update(expected)
        return [{'path': path} for path in expected]


@pytest.mark.asyncio
async def test_a_restart_reuses_the_global_part_only_for_the_same_conversation(monkeypatch):
    phases: list[int] = []

    async def phase(*, inspection, **_kwargs):
        phases.append(1)
        inspection.inputs['README.md'] = 'a' * 64
        return global_part(), {}

    monkeypatch.setattr(discovery_module, '_phase', phase)
    budget = AttemptBudget(max_calls=1024, max_reads=8192, max_coverage_revisions=8)
    messages = [{'role': 'system', 'content': 'author'}, {'role': 'user', 'content': '{}'}]
    first = SimpleNamespace(budget=budget, chat=None, messages=messages, discovery=BindingInspection())
    assert await discovery_module._global_part(first) == global_part()
    resumed = SimpleNamespace(budget=budget, chat=None, messages=json.loads(json.dumps(messages)),
                              discovery=BindingInspection())
    assert await discovery_module._global_part(resumed) == global_part()
    assert phases == [1]
    assert resumed.discovery.bound == [{'README.md': 'a' * 64}]
    changed = SimpleNamespace(budget=budget, chat=None, messages=[*messages, {'role': 'user', 'content': 'revise'}],
                              discovery=BindingInspection())
    await discovery_module._global_part(changed)
    assert phases == [1, 1]


@pytest.mark.asyncio
async def test_a_restart_reuses_a_check_groups_part_only_for_the_same_brief(monkeypatch):
    from coordinare.services import environment_shard_construction as construction_module
    from coordinare.services.environment_shard_construction import DividedConstruction
    from coordinare.services.environment_shard_scope import AgentContext

    agents: list[str] = []

    async def agent(_context, inspection, _messages, task):
        agents.append(task.envelope)
        inspection.inputs['src/001.md'] = 'b' * 64
        return task.validate(group_reply(group))

    monkeypatch.setattr(construction_module, 'SourceInspection', BindingInspection)
    monkeypatch.setattr(construction_module, 'run_agent', agent)
    group = CheckGroup('c00-', (make_criterion(1),))
    budget = AttemptBudget(max_calls=1024, max_reads=8192, max_coverage_revisions=8)
    scope = SimpleNamespace(all_inputs={'src/001.md': 'b' * 64})

    def builder():
        return DividedConstruction(AgentContext(budget, None, None), [], None, scope, 4)

    first = await builder().author(group, summary=SUMMARY, probes=[])
    restarted = builder()
    assert await restarted.author(group, summary=SUMMARY, probes=[]) == first
    assert agents == ['shard_checks']
    assert restarted.group_inputs == {'src/001.md': 'b' * 64}
    revised = builder()
    revised.feedback = {'c00-': {'merge_errors': [{'code': 'executable_merge_collision'}]}}
    await revised.author(group, summary=SUMMARY, probes=[])
    assert agents == ['shard_checks', 'shard_checks']


def test_executable_parts_survive_a_restart_without_renewing_the_budget(tmp_path):
    from coordinare.services.environment_repair import begin_attempt, save_attempt
    from tests.unit.test_353_execution_repair import cache_fixture
    from tests.unit.test_353_shard_scope import COMMIT, LIMITS

    cache = cache_fixture(tmp_path)
    cache.bootstrap_commit_sha = COMMIT
    budget = begin_attempt(cache, LIMITS, outer_seconds=600)
    budget.reserve_call()
    budget.executable_parts = {'groups': {'c00-': {'key': 'k' * 64, 'data': {}, 'inputs': {}}}}
    save_attempt(cache, budget)
    resumed = begin_attempt(cache, LIMITS, outer_seconds=600)
    assert resumed.executable_parts == budget.executable_parts
    assert resumed.calls == budget.calls == 1
