"""Unresolved source applicability returns review feedback before contract admission."""
from __future__ import annotations

import hashlib
import json

import pytest
from coordinare_service_inference.environment.findings import FindingLedger, FindingReview
from coordinare_service_inference.environment.scope import (
    ScopeAssessmentProposal,
    ScopeCriterion,
    ScopeCriterionProposal,
    ScopeDefinition,
    SourceAssessmentRecord,
    criterion_id,
)

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_scope import _accept_scope, _Request, _ResponseReferenceError
from coordinare.services.environment_shard_scope import ReviewMaterial, ShardedScope
from tests.unit.test_353_scope_contract import Replies, criterion, proposal, review, revision, run
from tests.unit.test_353_shard_review_fixes import definition, full
from tests.unit.test_353_shard_scope import COMMIT, ENTRIES, LIMITS, PLAN, SOURCES


@pytest.mark.asyncio
async def test_final_contract_guard_still_refuses_unknown_even_without_review_feedback():
    base = definition(full('README.md', 'unresolved', applicability='unknown'))
    budget = AttemptBudget()
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await _accept_scope(budget, definition=base, reviewer=None, record=None)
    assert failure.value.code == 'scope_applicability_unknown'
    assert budget.scope_contract is None


@pytest.mark.asyncio
@pytest.mark.parametrize('resumed', [False, True])
async def test_unknown_acceptance_is_corrected_then_reassessed_without_losing_required_scope(resumed):
    retained = criterion(usability='The established local operation succeeds')
    unresolved = criterion(applicability='unknown', usability='The optional seed entry point is unresolved')
    resolved = criterion(usability='The documented local seed operation succeeds')
    unknown_id = criterion_id(ScopeCriterionProposal.model_validate(unresolved))
    retained_id = criterion_id(ScopeCriterionProposal.model_validate(retained))

    def actionable_review(messages):
        errors = json.loads(messages[-1]['content'])['validation_errors']
        assert errors[0]['code'] == 'scope_applicability_unknown'
        assert errors[0]['unknown_criterion_ids'] == [unknown_id]
        assert 'unknown_applicability' in errors[0]['guidance']
        return review('revise', findings=[{
            'category': 'coverage', 'issue': 'The local seed applicability needs source resolution',
            'sources': ['guide'], 'affected_ids': [unknown_id],
            'gap_kind': 'unknown_applicability', 'inspection_requests': [],
        }])

    values = [
        review(), actionable_review,
        revision(reassess=[{'criterion_id': unknown_id, 'criterion': resolved}]),
        review(decisions=[{'finding_id': 'F0001', 'status': 'resolved',
            'rationale': 'The seed obligation is now source-backed and required', 'sources': ['guide']}]),
    ]
    budget = AttemptBudget()
    if resumed:
        from tests.unit.test_353_scope_contract import COMMIT as SOURCE_COMMIT
        from tests.unit.test_353_scope_contract import SOURCES as SOURCE_FILES

        inputs = {'guide': hashlib.sha256(SOURCE_FILES['guide'].encode()).hexdigest()}
        proposals = [ScopeCriterionProposal.model_validate(value) for value in (retained, unresolved)]
        base = ScopeDefinition(source_commit=SOURCE_COMMIT,
            inventory_digest=hashlib.sha256(json.dumps(sorted(SOURCE_FILES), separators=(',', ':')).encode()).hexdigest(),
            inputs=inputs, criteria=[ScopeCriterion(**value.model_dump(), id=criterion_id(value)) for value in proposals])
        budget.scope_definition = base
        budget.scope_inputs = dict(inputs)
        budget.scope_phase = 'review'
        budget.scope_revisions = 1
        budget.scope_findings = FindingLedger(source_commit=SOURCE_COMMIT, reviews=[FindingReview(
            sequence=1, manifest_digest=base.digest(), inputs=inputs, decisions=[], added_ids=[])])
        budget.scope_assessment = SourceAssessmentRecord(review_sequence=1, scope_digest=base.digest(),
            source_commit=SOURCE_COMMIT, inventory_digest=base.inventory_digest, reviewer_inputs=inputs,
            assessment=ScopeAssessmentProposal.model_validate(review()['scope_assessment']))
    else:
        values[:0] = [{'tool': 'read_file', 'path': 'guide'}, proposal(retained, unresolved)]
    replies = Replies(values)
    scope = await run(replies, budget=budget)
    assert retained_id in {item.id for item in scope.definition.criteria}
    assert all(item.applicability == 'required' for item in scope.definition.criteria)
    assert scope.finding_ledger.open_ids() == []
    assert len(scope.finding_ledger.reviews) == 2 + int(resumed)
    assert budget.schema_repairs == 1 and budget.scope_revisions == 1 + int(resumed)
    assert budget.probe_batches == 0 and budget.coverage_manifest is None


@pytest.mark.parametrize('owns_unknown', [True, False])
def test_sharded_acceptance_checks_only_the_judges_assigned_criteria(owns_unknown):
    async def fetch(path):
        return SOURCES.get(path)

    budget = AttemptBudget()
    budget.scope_findings = FindingLedger(source_commit=COMMIT)
    scope = ShardedScope(_Request(list(SOURCES), fetch, None, COMMIT, LIMITS, budget, entries=ENTRIES),
                         ENTRIES, PLAN, 4)
    inspection = scope.view(name='scope_shard_review')
    inspection.admit_content('README.md', SOURCES['README.md'], fetched=False)
    unresolved = full('README.md', 'unresolved', applicability='unknown')
    known = full('svc0/file0.txt', 'known')
    base = definition(unresolved, known)
    material = ReviewMaterial([unresolved] if owns_unknown else [known], [], [], 4)
    data = {'scope_assessment': {'verdict': 'accept', 'rationale': 'Inspected assigned source obligations',
        'sources': ['README.md'], 'findings': [], 'decisions': []}}
    if owns_unknown:
        with pytest.raises(_ResponseReferenceError) as failure:
            scope._validate_part(data, inspection=inspection, definition=base, material=material)
        assert failure.value.errors[0]['code'] == 'scope_applicability_unknown'
        assert failure.value.errors[0]['unknown_criterion_ids'] == [unresolved.id]
        assert budget.scope_findings.reviews == []
    else:
        assessment, _inputs = scope._validate_part(data, inspection=inspection, definition=base, material=material)
        assert assessment.verdict == 'accept'
