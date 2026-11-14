"""Conflict repairs retain the complete editable side of a source contradiction."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest
from coordinare_service_inference.environment.findings import FindingLedger
from coordinare_service_inference.environment.scope import ScopeDefinition

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_scope import (
    _Request,
    _ResponseReferenceError,
    _validate_review,
)
from coordinare.services.environment_shard_scope import Assignment, ShardedScope, criterion_owner
from tests.unit.test_353_shard_review_fixes import full
from tests.unit.test_353_shard_scope import COMMIT, ENTRIES, LIMITS, PLAN, SOURCES


@pytest.mark.asyncio
@pytest.mark.parametrize('retained', [False, True])
@pytest.mark.parametrize('cross_owner', [False, True])
@pytest.mark.parametrize('gap_kind', ['source_conflict', 'missing_obligation'])
@pytest.mark.parametrize('context_overflow', [False, True])
async def test_conflict_repair_exposes_editable_counterpart_and_preserves_required_freeze(
    retained, cross_owner, gap_kind, context_overflow,
):
    path = 'svc1/file0.txt'
    required = full(path, 'Run the local audit')
    editable = full(path, 'Exclude the local audit ' + 'details ' * 25, applicability='not_applicable',
                    sources=['svc0/file0.txt', path] if cross_owner else [path],
                    rationale='The exclusion must distinguish an optional remote refresh from a local scan')
    unrelated = full('svc2/file0.txt', 'Unrelated optional deployment', applicability='not_applicable')
    protected = full('svc2/file1.txt', 'Unrelated required operation')
    replacement = editable.model_dump(exclude={'id'}) | {
        'description': 'Only the optional remote refresh is excluded',
        'rationale': 'A local scan remains required; installation can supply its input',
    }
    budget = AttemptBudget(max_calls=32, max_reads=128)

    async def fetch(name):
        return SOURCES.get(name)

    async def chat(messages, *, response_schema):
        context = json.loads(messages[1]['content'])
        criteria = {item['id']: item for item in context['scope_criteria']}
        assert criteria[required.id] == required.model_dump(mode='json')
        assert unrelated.id not in criteria
        if gap_kind == 'source_conflict':
            # This fails when only the named, frozen requirement reaches the repair author.
            assert criteria[editable.id] == editable.model_dump(mode='json')
            changes = [{'criterion_id': editable.id, 'criterion': replacement}]
        else:
            assert editable.id not in criteria
            changes = []
        return json.dumps({'scope_revision': {'base_digest': base.digest(), 'add': [], 'reassess': changes},
            'finding_responses': [{'finding_id': 'F0001', 'rationale': 'Clarified the optional exclusion',
                                  'sources': [path], 'affected_ids': [required.id]}]})

    limits = replace(LIMITS, max_review_context_bytes=512) if context_overflow else LIMITS
    scope = ShardedScope(_Request(list(SOURCES), fetch, chat, COMMIT, limits, budget, entries=ENTRIES),
                         ENTRIES, PLAN, 4)
    reviewer = scope.view(name='scope_review')
    for name in {p for c in [required, editable, unrelated, protected] for p in c.sources}:
        reviewer.admit_content(name, SOURCES[name], fetched=False)
    base = ScopeDefinition(source_commit=COMMIT, inventory_digest=reviewer.inventory_digest,
                           inputs=dict(reviewer.inputs), criteria=[required, editable, unrelated, protected])
    assessment = {'scope_assessment': {'verdict': 'revise', 'rationale': 'Resolve conflicting local audit scope',
        'sources': [path], 'findings': [{'category': 'coverage', 'issue': 'Conflicting local audit scope',
        'sources': [path], 'affected_ids': [required.id], 'gap_kind': gap_kind, 'inspection_requests': []}],
        'decisions': []}}
    _, ledger, record = _validate_review(assessment, reviewer=reviewer, definition=base,
                                        ledger=FindingLedger(source_commit=COMMIT), commit=COMMIT)
    if retained:
        assessment['scope_assessment']['findings'] = []
        assessment['scope_assessment']['decisions'] = [{'finding_id': 'F0001', 'disposition': 'source_gap',
            'rationale': 'The conflict remains', 'sources': [path], 'affected_ids': [required.id],
            'gap_kind': gap_kind, 'inspection_requests': []}]
        _, ledger, record = _validate_review(assessment, reviewer=reviewer, definition=base,
                                            ledger=ledger, commit=COMMIT)
    budget.scope_inputs = dict(reviewer.inputs)
    budget.scope_findings = ledger
    budget.scope_assessment = record
    budget.scope_revision_base_digest = base.digest()
    budget.scope_phase = 'revision'
    if context_overflow:
        with pytest.raises(EnvironmentDiscoveryError) as overflow:
            await scope.revision_round(base)
        assert overflow.value.code == 'shard_context_limit'
        assert budget.calls_completed == 0 and budget.probe_batches == 0
        assert budget.scope_findings.open_ids() == ['F0001']
        assert budget.scope_phase == 'revision'
        return
    revised = await scope.revision_round(base)
    assert next(c for c in revised.criteria if c.id == required.id) == required
    assert budget.scope_findings.open_ids() == ['F0001']  # Only a fresh reviewer can close it.
    assert budget.scope_phase == 'review' and budget.probe_batches == 0
    if gap_kind == 'source_conflict':
        assert editable.id not in {c.id for c in revised.criteria}
        assert any(c.description == replacement['description'] for c in revised.criteria)
    inspection = scope.view(name='scope_revision')
    inspection.admit_content(protected.sources[0], SOURCES[protected.sources[0]], fetched=False)
    with pytest.raises(_ResponseReferenceError) as failure:
        scope._validate_group({'scope_revision': {'base_digest': base.digest(), 'add': [], 'reassess': [
            {'criterion_id': protected.id, 'criterion': protected.model_dump(exclude={'id'})}]},
            'finding_responses': []}, inspection=inspection, base=base,
            assignment=Assignment(criterion_owner(protected, scope.work), ('F0001',), []))
    assert failure.value.errors[0]['reason'] == 'required'
