"""Guided sharding: two independent listing triages decide what is read; every exclusion is reviewable."""
from __future__ import annotations

import json

import pytest
from coordinare_service_inference.environment.scope import ScopeDefinition, TriageVerdict

from coordinare.services.environment_attempt import AttemptBudget
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_inspection import SourceInspection
from coordinare.services.environment_shard_triage import exclusions
from tests.unit.test_353_shard_scope import COMMIT, ENTRIES, SOURCES, Router, criterion, run


class Sides(Router):
    """The first triage of each listing shard is one side, the second the other; order is not assumed."""

    def __init__(self, first=(), second=(), *, incomplete_once=False, reference=None):
        super().__init__()
        self.sides = [tuple(first), tuple(second)]
        self.calls: dict[str, int] = {}
        self.incomplete_once = incomplete_once
        self.reference = reference

    def triage(self, messages, user):
        if self.incomplete_once and len(messages) == 2:
            self.incomplete_once = False
            return {'listing_triage': {'verdicts': [{'path': 'svc0', 'verdict': 'environment', 'rationale': 'r'}]}}
        if len(messages) == 2:
            self.calls[user['listing_shard']] = self.calls.get(user['listing_shard'], 0) + 1
        self.unrelated = self.sides[min(self.calls.get(user['listing_shard'], 1), 2) - 1]
        return super().triage(messages, user)

    def author(self, messages, user):
        reply = super().author(messages, user)
        if self.reference and 'shard_summary' in reply and any(p.startswith('svc0') for p in
                                                               [c['sources'][0] for c in reply['shard_summary']['criteria']]):
            reply['shard_summary']['cross_references'] = [self.reference]
        if self.reference and '.r' in user['shard']['id'] and len(messages) == 2:
            return {'tool': 'read_file', 'path': self.reference}
        if self.reference and '.r' in user['shard']['id']:
            return {'shard_summary': {'criteria': [criterion(self.reference, 'referenced')], 'cross_references': [], 'questions': []}}
        return reply


def authored_paths(router):
    return {path for role, messages in router.conversations if role == 'author'
            for path in [m for m in json.dumps(messages).split('"') if m.startswith('svc')]}


@pytest.mark.asyncio
async def test_an_area_is_excluded_only_when_both_triages_judge_it_unrelated():
    both = Sides(first=('svc2',), second=('svc2',))
    contract = await run(both)
    assert not [p for p in contract.definition.inputs if p.startswith('svc2')]
    [record] = contract.definition.exclusions
    assert (record.path, record.files) == ('svc2', 3)
    [merge] = [json.loads(m[1]['content']) for role, m in both.conversations if role == 'merge']
    assert merge['exclusions'][0]['path'] == 'svc2'

    one = Sides(first=('svc2',), second=())
    contract = await run(one)
    assert [p for p in contract.definition.inputs if p.startswith('svc2')] and not contract.definition.exclusions


@pytest.mark.asyncio
async def test_an_uncovered_listing_is_a_correctable_error():
    budget = AttemptBudget(max_calls=1024, max_reads=8192, max_coverage_revisions=8)
    await run(Sides(incomplete_once=True), budget=budget)
    assert budget.shard_schema_repairs == 1 and 'triage_incomplete' not in budget.validation_codes[1:]


@pytest.mark.asyncio
async def test_a_referenced_file_in_an_excluded_area_is_still_read():
    contract = await run(Sides(first=('svc2',), second=('svc2',), reference='svc2/file1.txt'))
    assert 'svc2/file1.txt' in contract.definition.inputs
    [record] = contract.definition.exclusions
    assert (record.path, record.files) == ('svc2', 2)


def test_no_exclusions_keeps_existing_digests():
    from tests.unit.test_353_shard_review_fixes import definition, full
    plain = definition(full('svc0/file0.txt', 'x'))
    assert 'exclusions' not in plain.model_dump() and ScopeDefinition.model_validate(plain.model_dump()).digest() == plain.digest()


def test_spine_files_are_never_excluded():
    verdict = TriageVerdict(path='README.md', verdict='unrelated', rationale='r')
    excluded, records = exclusions(['README.md'], spine=['README.md'], sides=[{'README.md': verdict}, {'README.md': verdict}])
    assert not excluded and not records


@pytest.mark.asyncio
async def test_a_stray_cursor_on_a_read_is_ignored():
    async def fetch(path):
        return SOURCES.get(path)
    inspection = SourceInspection(ENTRIES, fetch, DiscoveryLimits(), AttemptBudget())
    result = await inspection.tool_result({'tool': 'read_files', 'paths': ['README.md'], 'cursor': 0})
    assert result['files'][0]['path'] == 'README.md'


@pytest.mark.asyncio
async def test_triage_is_not_repeated_after_a_restart(tmp_path):
    from tests.unit.test_353_shard_recovery import flow, fresh_cache
    cache = fresh_cache(tmp_path)
    await flow(cache, Router(), stop_when=lambda budget: len((budget.scope_shards or {}).get('triage', {}).get('review', {})) >= 1)
    second = Router()
    await flow(cache, second)
    assert not [role for role, _messages in second.conversations if role == 'triage']
    assert COMMIT


def verdict(path, value):
    return TriageVerdict(path=path, verdict=value, rationale='r')


def test_a_repeated_path_in_one_triage_is_a_correctable_error():
    from coordinare.services.environment_scope import _ResponseReferenceError
    from coordinare.services.environment_shard_triage import validate_triage
    from coordinare.services.environment_shards import Shard
    shard = Shard(id='shard-000-00000000', paths=('a/x.yml', 'a/y.md'), readable_bytes=0, depth=0)
    data = {'listing_triage': {'verdicts': [{'path': 'a', 'verdict': 'environment', 'rationale': 'r'},
                                            {'path': 'a', 'verdict': 'unrelated', 'rationale': 'r'}]}}
    with pytest.raises(_ResponseReferenceError) as failure:
        validate_triage(data, shard=shard)
    assert failure.value.errors[0]['code'] == 'triage_path_repeated'


def test_a_directory_judged_differently_across_listing_shards_keeps_the_inclusive_verdict():
    from coordinare.services.environment_shard_triage import merge_side
    merged = merge_side([{'a': verdict('a', 'environment')}, {'a': verdict('a', 'unrelated')}])
    assert merged['a'].verdict == 'environment'
    side = merged
    excluded, _ = exclusions(['a/x.yml', 'a/y.md'], spine=[], sides=[side, {'a': verdict('a', 'unrelated')}])
    assert not excluded


def test_exclusion_records_stay_within_their_bound_with_exact_counts():
    paths = [f'g{i % 10}/sub{i}/file.txt' for i in range(600)]
    side = {f'g{i % 10}/sub{i}': verdict(f'g{i % 10}/sub{i}', 'unrelated') for i in range(600)}
    excluded, records = exclusions(paths, spine=[], sides=[side, side])
    assert len(records) == 10 and sum(record.files for record in records) == len(excluded) == 600
    # More unrelated top-level areas than the record can hold: everything is read instead.
    wide = {f'g{i}': verdict(f'g{i}', 'unrelated') for i in range(600)}
    assert exclusions([f'g{i}/f.txt' for i in range(600)], spine=[], sides=[wide, wide]) == (set(), [])


@pytest.mark.asyncio
async def test_exclusions_shrink_when_an_investigation_reads_an_excluded_file():
    from tests.unit.test_353_shard_review_fixes import InvestigatingRouter

    class Both(InvestigatingRouter):
        unrelated = ('svc2',)

    contract = await run(Both())
    assert 'svc2/file2.txt' in contract.definition.inputs
    [record] = contract.definition.exclusions
    assert (record.path, record.files) == ('svc2', 2)


@pytest.mark.asyncio
async def test_an_authored_shard_that_read_nothing_is_still_reviewed():
    class Silent(Router):
        def author(self, messages, user):
            return {'shard_summary': {'criteria': [], 'cross_references': [], 'questions': []}} if 'svc1' in json.dumps(
                user['root_directory']) and user['shard']['id'] == self._svc1 else super().author(messages, user)

    from tests.unit.test_353_shard_scope import PLAN
    silent = Silent()
    silent._svc1 = next(shard.id for shard in PLAN.shards if shard.paths[0].startswith('svc1'))
    await run(silent)
    first_round = [m for role, m in silent.conversations if role == 'review']
    assert len(first_round) >= len(PLAN.shards)
