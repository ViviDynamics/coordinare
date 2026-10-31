"""Nested source citations are corrected by their reviewer before shard aggregation."""
from __future__ import annotations

import copy
import json

import pytest
from coordinare_service_inference.environment.findings import FindingLedger

from coordinare.services.environment_attempt import AttemptBudget
from coordinare.services.environment_scope import _Request, _ResponseReferenceError
from coordinare.services.environment_shard_scope import ReviewMaterial, ShardedScope
from tests.unit.test_353_shard_review_fixes import definition, full
from tests.unit.test_353_shard_scope import COMMIT, ENTRIES, LIMITS, PLAN, SOURCES, decision


def setup_review(nested, chat=None):
    async def fetch(path):
        return SOURCES.get(path)

    budget = AttemptBudget()
    budget.scope_findings = FindingLedger(source_commit=COMMIT)
    scope = ShardedScope(_Request(list(SOURCES), fetch, chat, COMMIT, LIMITS, budget, entries=ENTRIES),
                         ENTRIES, PLAN, 4)
    inspection = scope.view(name='scope_shard_review')
    inspection.admit_content('README.md', SOURCES['README.md'], fetched=False)
    item = full('README.md', 'x')
    finding = {'category': 'coverage', 'issue': 'Missing obligation', 'sources': ['README.md'],
               'affected_ids': [item.id], 'gap_kind': 'missing_obligation', 'inspection_requests': []}
    data = {'scope_assessment': {'verdict': 'revise' if nested == 'findings' else 'accept',
        'rationale': 'Independent review', 'sources': ['README.md'],
        'findings': [finding] if nested == 'findings' else [],
        'decisions': [decision('F0001', [item.id])] if nested == 'decisions' else []}}
    material = ReviewMaterial([item], ['F0001'] if nested == 'decisions' else [], [], 1)
    return scope, inspection, definition(item), material, data


@pytest.mark.parametrize('nested', ['findings', 'decisions'])
@pytest.mark.parametrize('source', ['svc0/file0.txt', 'svc0'])
def test_part_rejects_unread_nested_sources_before_aggregation(nested, source):
    scope, inspection, base, material, data = setup_review(nested)
    data['scope_assessment'][nested][0]['sources'] = [source]
    with pytest.raises(_ResponseReferenceError) as failure:
        scope._validate_part(data, inspection=inspection, definition=base, material=material)
    [error] = failure.value.errors
    assert error['code'] == 'scope_citations_invalid'
    assert error['location'] == ['scope_assessment', nested, '0', 'sources', '0']
    assert error['invalid_source_indices'] == ['0']
    assert error['inspected_files'] == ['README.md']
    assert source not in json.dumps(error)


@pytest.mark.asyncio
@pytest.mark.parametrize('nested', ['findings', 'decisions'])
async def test_owning_reviewer_corrects_nested_citation_without_scope_revision(nested):
    calls = []
    scope, inspection, base, material, valid = setup_review(nested)
    invalid = copy.deepcopy(valid)
    invalid['scope_assessment'][nested][0]['sources'] = ['svc0']

    async def chat(messages, *, response_schema):
        calls.append(copy.deepcopy(messages))
        return json.dumps(invalid if len(calls) == 1 else valid)

    from dataclasses import replace

    scope.request = replace(scope.request, chat=chat)
    assessment, inputs = await scope._judge(inspection, [{'role': 'system', 'content': 'Independent review'},
        {'role': 'user', 'content': '{}'}], definition=base, material=material)
    assert assessment.verdict == valid['scope_assessment']['verdict']
    assert getattr(assessment, nested)[0].sources == ['README.md']
    assert inputs.keys() == {'README.md'}
    assert len(calls) == 2 and scope.budget.shard_schema_repairs == 1
    assert scope.budget.scope_revisions == scope.budget.coverage_revisions == 0
    correction = json.loads(calls[1][-1]['content'])
    assert correction['validation_errors'][0]['location'] == ['scope_assessment', nested, '0', 'sources', '0']
