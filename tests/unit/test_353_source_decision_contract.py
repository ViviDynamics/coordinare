"""Source review decisions carry one explicit disposition and derive generic ledger status."""
from __future__ import annotations

import hashlib
import json

import pytest
from coordinare_service_inference.environment.findings import FindingDecisionProposal, FindingLedger
from coordinare_service_inference.environment.models import CoverageAssessmentProposal
from coordinare_service_inference.environment.scope import (
    ScopeAssessmentProposal,
    ScopeContract,
    ScopeCriterion,
    ScopeDefinition,
    ScopeFindingProposal,
    SourceAssessmentRecord,
    canonical,
    criterion_id,
    derive_finding_decision,
    validate_source_assessment,
)
from pydantic import TypeAdapter, ValidationError

from coordinare.services.environment_findings import ReviewSubject, apply_evidence_review

COMMIT = 'a'*40
INVENTORY = 'b'*64
INPUTS = {'guide': 'c'*64, 'odd/nest': 'd'*64}


def _definition() -> ScopeDefinition:
    from coordinare_service_inference.environment.scope import ScopeCriterionProposal
    proposal = ScopeCriterionProposal(description='Development environment', sources=['guide'],
        applicability='required', rationale='Documented obligation',
        usability='The documented development command succeeds', runtime_needs=[])
    return ScopeDefinition(source_commit=COMMIT, inventory_digest=INVENTORY, inputs=dict(INPUTS),
        criteria=[ScopeCriterion(**proposal.model_dump(), id=criterion_id(proposal))])


def _decision(**value: object) -> object:
    from coordinare_service_inference.environment.scope import SourceFindingDecision
    base = {'finding_id': 'F0001', 'rationale': 'Inspected source evidence', 'sources': ['guide']}
    return TypeAdapter(SourceFindingDecision).validate_python({**base, **value})


def _finding(**value: object) -> dict[str, object]:
    return {'category': 'coverage', 'issue': 'Nested service obligation is unread', 'sources': ['guide'],
            'affected_ids': [], 'gap_kind': 'unread_source', 'inspection_requests': [], **value}


def _assessment(verdict: str, *, findings=(), decisions=()) -> ScopeAssessmentProposal:
    return ScopeAssessmentProposal.model_validate({'verdict': verdict, 'rationale': 'Independent source judgment',
        'sources': ['guide'], 'findings': list(findings), 'decisions': list(decisions)})


def _apply(ledger: FindingLedger, definition: ScopeDefinition, assessment: ScopeAssessmentProposal) -> FindingLedger:
    proposal = CoverageAssessmentProposal(verdict=assessment.verdict, rationale=assessment.rationale,
        sources=assessment.sources, capability_unavailable=False,
        findings=[f.model_dump(exclude={'inspection_requests', 'gap_kind'}) for f in assessment.findings],
        decisions=[derive_finding_decision(d).model_dump() for d in assessment.decisions])
    return apply_evidence_review(ledger, subject=ReviewSubject(COMMIT, definition.digest(),
        {c.id for c in definition.criteria}), proposal=proposal, reviewer_inputs=INPUTS)


def _record(sequence: int, definition: ScopeDefinition, assessment: ScopeAssessmentProposal) -> SourceAssessmentRecord:
    return SourceAssessmentRecord(review_sequence=sequence, scope_digest=definition.digest(), source_commit=COMMIT,
        inventory_digest=INVENTORY, reviewer_inputs=dict(INPUTS), assessment=assessment)


def _two_reviews():
    definition = _definition()
    first = _assessment('revise', findings=[_finding()])
    ledger = _apply(FindingLedger(source_commit=COMMIT), definition, first)
    second = _assessment('accept', decisions=[{'finding_id': 'F0001', 'disposition': 'covered',
        'criterion_ids': [definition.criteria[0].id], 'rationale': 'Criterion states the success condition',
        'sources': ['guide']}])
    ledger = _apply(ledger, definition, second)
    return definition, ledger, first, second


def test_each_disposition_derives_generic_status():
    covered = derive_finding_decision(_decision(disposition='covered', criterion_ids=['S'+'0'*16]))
    correction = derive_finding_decision(_decision(disposition='source_correction'))
    gap = derive_finding_decision(_decision(disposition='source_gap', gap_kind='unread_source',
                                            affected_ids=[], inspection_requests=[]))
    duplicate = derive_finding_decision(_decision(finding_id='F0002', disposition='duplicate', duplicate_of='F0001'))
    expected = {'rationale': 'Inspected source evidence', 'sources': ['guide']}
    assert covered == FindingDecisionProposal(finding_id='F0001', status='resolved', duplicate_of=None, **expected)
    assert correction == FindingDecisionProposal(finding_id='F0001', status='resolved', duplicate_of=None, **expected)
    assert gap == FindingDecisionProposal(finding_id='F0001', status='open', duplicate_of=None, **expected)
    assert duplicate == FindingDecisionProposal(finding_id='F0002', status='resolved', duplicate_of='F0001', **expected)


@pytest.mark.parametrize('value', [
    {'disposition': 'covered', 'criterion_ids': ['S'+'0'*16], 'status': 'resolved'},
    {'status': 'open'},
    {'disposition': 'source_gap', 'gap_kind': 'pending_execution', 'affected_ids': [], 'inspection_requests': []},
    {'disposition': 'source_gap', 'gap_kind': 'awaiting_scope_acceptance', 'affected_ids': [], 'inspection_requests': []},
])
def test_rejects_authored_status_and_runtime_wait_kinds(value):
    with pytest.raises(ValidationError):
        _decision(**value)


@pytest.mark.parametrize('value', [
    {'disposition': 'covered', 'criterion_ids': ['S'+'0'*16]},
    {'disposition': 'source_correction'},
    {'disposition': 'duplicate', 'duplicate_of': 'F0002'},
])
def test_resolving_dispositions_reject_inspection_requests(value):
    with pytest.raises(ValidationError):
        _decision(**value, inspection_requests=[{'operation': 'read_file', 'path': 'odd/nest'}])


@pytest.mark.parametrize('ids', [[], ['S'+'0'*16, 'S'+'0'*16]])
def test_covered_requires_unique_criteria(ids):
    with pytest.raises(ValidationError):
        _decision(disposition='covered', criterion_ids=ids)


@pytest.mark.parametrize('sources', [[], ['guide', 'guide']])
def test_decision_citations_nonempty_unique(sources):
    with pytest.raises(ValidationError):
        _decision(disposition='source_correction', sources=sources)


def test_new_finding_requires_gap_kind():
    with pytest.raises(ValidationError):
        ScopeFindingProposal.model_validate({k: v for k, v in _finding().items() if k != 'gap_kind'})
    with pytest.raises(ValidationError):
        ScopeFindingProposal.model_validate(_finding(gap_kind='pending_execution'))


def test_assessment_binding_matches_ledger_review():
    definition, ledger, first, second = _two_reviews()
    validate_source_assessment(_record(1, definition, first), ledger=ledger)
    validate_source_assessment(_record(2, definition, second), ledger=ledger)
    good = _record(2, definition, second)
    changed_decision = second.model_copy(update={'decisions': [_decision(disposition='source_gap',
        gap_kind='unread_source', affected_ids=[], inspection_requests=[])]})
    changed_finding = first.model_copy(update={'findings': [ScopeFindingProposal.model_validate(
        _finding(issue='A different objection'))]})
    variants = [good.model_copy(update={'review_sequence': 3}),
                good.model_copy(update={'scope_digest': 'e'*64}),
                good.model_copy(update={'reviewer_inputs': {**INPUTS, 'guide': 'f'*64}}),
                good.model_copy(update={'source_commit': 'b'*40}),
                good.model_copy(update={'assessment': changed_decision}),
                _record(1, definition, changed_finding)]
    for variant in variants:
        with pytest.raises(ValueError, match='source assessment'):
            validate_source_assessment(variant, ledger=ledger)


def _contract(definition, ledger, record, **update):
    return {'definition': definition.model_dump(mode='json'), 'review_inputs': dict(INPUTS), 'sources': ['guide'],
            'finding_ledger': ledger.model_dump(mode='json'), 'finding_responses': [],
            'source_assessment': record.model_dump(mode='json') if record is not None else None, **update}


def test_contract_v2_requires_accepting_latest_assessment():
    definition, ledger, first, second = _two_reviews()
    contract = ScopeContract.model_validate(_contract(definition, ledger, _record(2, definition, second)))
    assert contract.schema_version == 2 and contract.current
    for value in [_contract(definition, ledger, None),
                  _contract(definition, ledger, _record(1, definition, first)),
                  _contract(definition, ledger, _record(2, definition, second), sources=['odd/nest']),
                  _contract(definition, ledger, _record(2, definition, second), review_inputs={**INPUTS, 'extra': 'e'*64})]:
        with pytest.raises(ValidationError):
            ScopeContract.model_validate(value)


def test_v1_contract_parses_for_audit_with_unchanged_digest():
    definition, ledger, _, _ = _two_reviews()
    legacy = _contract(definition, ledger, None, schema_version=1)
    del legacy['source_assessment']
    contract = ScopeContract.model_validate(legacy)
    assert not contract.current
    assert contract.model_dump(mode='json') == legacy
    assert contract.digest() == hashlib.sha256(canonical(legacy)).hexdigest()
    with pytest.raises(ValidationError):
        ScopeContract.model_validate({**legacy, 'source_assessment': _record(2, definition, _two_reviews()[3]).model_dump(mode='json')})


def test_contract_bound_with_large_assessment():
    definition = _definition()
    inputs = {**INPUTS, **{f'docs/page-{i:03}.md': f'{i:064x}' for i in range(118)}}
    text = 'x'*1500
    findings = [_finding(issue=text, sources=sorted(inputs)[:8]) for _ in range(20)]
    first = ScopeAssessmentProposal.model_validate({'verdict': 'revise', 'rationale': text, 'sources': ['guide'],
        'findings': findings, 'decisions': []})
    proposal = CoverageAssessmentProposal(verdict='revise', rationale=text, sources=['guide'], capability_unavailable=False,
        findings=[f.model_dump(exclude={'inspection_requests', 'gap_kind'}) for f in first.findings], decisions=[])
    subject = ReviewSubject(COMMIT, definition.digest(), {c.id for c in definition.criteria})
    ledger = apply_evidence_review(FindingLedger(source_commit=COMMIT), subject=subject, proposal=proposal, reviewer_inputs=inputs)
    decisions = [{'finding_id': f'F{i:04}', 'disposition': 'covered', 'criterion_ids': [definition.criteria[0].id],
                  'rationale': text, 'sources': ['guide']} for i in range(1, 21)]
    second = ScopeAssessmentProposal.model_validate({'verdict': 'accept', 'rationale': text, 'sources': ['guide'],
        'findings': [], 'decisions': decisions})
    proposal = CoverageAssessmentProposal(verdict='accept', rationale=text, sources=['guide'], capability_unavailable=False,
        findings=[], decisions=[derive_finding_decision(d).model_dump() for d in second.decisions])
    ledger = apply_evidence_review(ledger, subject=subject, proposal=proposal, reviewer_inputs=inputs)
    record = SourceAssessmentRecord(review_sequence=2, scope_digest=definition.digest(), source_commit=COMMIT,
        inventory_digest=INVENTORY, reviewer_inputs=inputs, assessment=second)
    contract = ScopeContract.model_validate(_contract(definition, ledger, record, review_inputs=inputs))
    assert len(json.dumps(contract.model_dump(mode='json')).encode()) <= 2*1024*1024
    oversized = record.model_copy(update={'reviewer_inputs': {**inputs, **{f'z/{i:05}': f'{i:064x}' for i in range(30000)}}})
    with pytest.raises(ValidationError, match='scope contract exceeds its bound'):
        ScopeContract.model_validate(_contract(definition, ledger, oversized, review_inputs=oversized.reviewer_inputs))
