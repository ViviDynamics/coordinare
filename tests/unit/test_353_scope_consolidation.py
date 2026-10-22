"""Over-fine scopes are consolidated into environment obligations without losing a citation or a requirement."""
from __future__ import annotations

import json

import pytest
from coordinare_service_inference.environment.scope import (
    ConsolidationGroup,
    ScopeCriterionProposal,
    criterion_id,
)

from coordinare.services.environment_attempt import AttemptBudget
from coordinare.services.environment_scope import (
    AUTHOR_INSTRUCTIONS,
    GRANULARITY_GUIDANCE,
    REVISION_INSTRUCTIONS,
    _ResponseReferenceError,
)
from coordinare.services.environment_shard_scope import (
    SHARD_AUTHOR_INSTRUCTIONS,
    apply_consolidation,
    validate_consolidation,
)
from tests.unit.test_353_shard_scope import Router, criterion, run


def proposal(path, label, **changes):
    return ScopeCriterionProposal(**{**criterion(path, label), **changes})


def group(members, merged):
    return {'member_ids': [criterion_id(member) for member in members], 'criterion': merged}


def test_every_author_gets_the_granularity_rule():
    for instructions in (AUTHOR_INSTRUCTIONS, REVISION_INSTRUCTIONS, SHARD_AUTHOR_INSTRUCTIONS):
        assert GRANULARITY_GUIDANCE in instructions


def test_a_group_keeps_every_citation_and_the_strongest_applicability():
    a = proposal('scripts/a.sh', 'a', applicability='unknown')
    b = proposal('scripts/b.sh', 'b')
    keep = criterion('scripts/a.sh', 'merged')
    with pytest.raises(_ResponseReferenceError) as failure:
        validate_consolidation({'scope_consolidation': {'groups': [group([a, b], keep)]}}, criteria=[a, b])
    assert {error['code'] for error in failure.value.errors} == {'consolidation_sources_invalid'}
    weak = {**criterion('scripts/a.sh', 'merged'), 'sources': ['scripts/a.sh', 'scripts/b.sh'], 'applicability': 'unknown'}
    with pytest.raises(_ResponseReferenceError) as failure:
        validate_consolidation({'scope_consolidation': {'groups': [group([a, b], weak)]}}, criteria=[a, b])
    assert failure.value.errors[0]['code'] == 'consolidation_applicability_invalid'
    good = {**weak, 'applicability': 'required'}
    [valid] = validate_consolidation({'scope_consolidation': {'groups': [group([a, b], good)]}}, criteria=[a, b])
    assert isinstance(valid, ConsolidationGroup)


def test_a_criterion_joins_at_most_one_group_and_groups_cite_at_most_32_sources():
    many = [proposal(f'scripts/{i}.sh', str(i)) for i in range(33)]
    big = {**criterion('scripts/0.sh', 'all'), 'sources': sorted(m.sources[0] for m in many)[:32]}
    with pytest.raises(_ResponseReferenceError) as failure:
        validate_consolidation({'scope_consolidation': {'groups': [group(many, big)]}}, criteria=many)
    assert failure.value.errors[0]['code'] == 'consolidation_group_too_large'
    a, b, c = many[:3]
    merged = {**criterion('scripts/0.sh', 'm'), 'sources': sorted(['scripts/0.sh', 'scripts/1.sh'])}
    merged2 = {**criterion('scripts/1.sh', 'm2'), 'sources': sorted(['scripts/1.sh', 'scripts/2.sh'])}
    with pytest.raises(_ResponseReferenceError) as failure:
        validate_consolidation({'scope_consolidation': {'groups': [group([a, b], merged), group([b, c], merged2)]}},
                               criteria=many)
    assert failure.value.errors[0]['code'] == 'consolidation_members_invalid'


def test_applying_groups_replaces_members_in_place_and_records_them():
    a, b, c = (proposal(f'scripts/{n}.sh', n) for n in 'abc')
    merged = ScopeCriterionProposal(**{**criterion('scripts/a.sh', 'ab'), 'sources': ['scripts/a.sh', 'scripts/b.sh']})
    result, records = apply_consolidation([a, b, c], [ConsolidationGroup(member_ids=[criterion_id(a), criterion_id(b)],
                                                                         criterion=merged)])
    assert [criterion_id(item) for item in result] == [criterion_id(merged), criterion_id(c)]
    assert records[0].member_ids == [criterion_id(a), criterion_id(b)]


class Many(Router):
    """Authors report many over-fine criteria; consolidation folds each shard's into one."""

    def __init__(self, *, garbled=False):
        super().__init__()
        self.garbled = garbled

    def author(self, messages, user):
        if len(messages) == 2:
            return {'tool': 'read_files', 'paths': [item['path'] for item in user['shard_files']][:8]}
        paths = [item['path'] for item in user['shard_files']]
        return {'shard_summary': {'criteria': [criterion(path, f'{path} detail {i}') for path in paths for i in range(6)],
                                  'cross_references': [], 'questions': []}}

    def consolidation(self, messages, user):
        if self.garbled:
            return {'scope_consolidation': {'groups': 'not a list'}}
        by_source: dict[str, list[dict]] = {}
        for item in user['criteria']:
            by_source.setdefault(item['sources'][0], []).append(item)
        groups = [{'member_ids': [item['id'] for item in items],
                   'criterion': {**criterion(source, f'{source} obligation')}}
                  for source, items in by_source.items() if len(items) >= 2]
        return {'scope_consolidation': {'groups': groups}}


@pytest.mark.asyncio
async def test_an_over_fine_scope_is_consolidated_before_review():
    router = Many()
    contract = await run(router)
    assert len(contract.definition.criteria) == 9 and len(contract.definition.consolidated) == 9
    assert all(len(record.member_ids) == 6 for record in contract.definition.consolidated)
    [merge] = [json.loads(m[1]['content']) for role, m in router.conversations if role == 'merge']
    assert 'cited_files' in merge


@pytest.mark.asyncio
async def test_an_unusable_consolidation_leaves_the_scope_as_authored():
    budget = AttemptBudget(max_calls=1024, max_reads=8192, max_coverage_revisions=8)
    contract = await run(Many(garbled=True), budget=budget)
    assert len(contract.definition.criteria) == 54 and not contract.definition.consolidated


def test_a_group_keeps_every_runtime_need():
    a = proposal('scripts/a.sh', 'a', runtime_needs=['postgres:16'])
    b = proposal('scripts/b.sh', 'b')
    dropped = {**criterion('scripts/a.sh', 'merged'), 'sources': ['scripts/a.sh', 'scripts/b.sh']}
    with pytest.raises(_ResponseReferenceError) as failure:
        validate_consolidation({'scope_consolidation': {'groups': [group([a, b], dropped)]}}, criteria=[a, b])
    assert failure.value.errors[0]['code'] == 'consolidation_runtime_needs_invalid'
    kept = {**dropped, 'runtime_needs': ['postgres:16']}
    assert validate_consolidation({'scope_consolidation': {'groups': [group([a, b], kept)]}}, criteria=[a, b])


@pytest.mark.asyncio
async def test_a_consolidation_that_runs_out_of_time_never_blocks_the_scope(monkeypatch):
    from coordinare.services.environment_attempt import EnvironmentDiscoveryError
    from coordinare.services.environment_shard_scope import ShardedScope

    async def stalled(self, chunk):
        raise EnvironmentDiscoveryError('phase deadline', code='scope_phase_deadline')
    monkeypatch.setattr(ShardedScope, 'consolidate_chunk', stalled)
    contract = await run(Many())
    assert len(contract.definition.criteria) == 54 and not contract.definition.consolidated


@pytest.mark.asyncio
async def test_the_merge_reviewer_sees_what_was_folded():
    router = Many()
    await run(router)
    [merge] = [json.loads(m[1]['content']) for role, m in router.conversations if role == 'merge']
    assert merge['consolidated'] and all(len(item['member_descriptions']) == 6 for item in merge['consolidated'])


@pytest.mark.asyncio
async def test_a_tool_request_from_a_listing_only_agent_is_corrected_not_executed():
    class Reader(Many):
        def consolidation(self, messages, user):
            if len(messages) == 2:
                return {'tool': 'read_file', 'path': 'README.md'}
            return super().consolidation(messages, user)

    budget = AttemptBudget(max_calls=1024, max_reads=8192, max_coverage_revisions=8)
    contract = await run(Reader(), budget=budget)
    assert contract.definition.consolidated and budget.shard_schema_repairs >= 1
