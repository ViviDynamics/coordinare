"""Scopes hold up to 512 criteria and ledgers 128 findings, within enlarged byte bounds."""
from __future__ import annotations

import json

import pytest
from coordinare_service_inference.environment.findings import (
    FindingLedger,
    FindingRecord,
    FindingReview,
)
from coordinare_service_inference.environment.scope import (
    MAX_SCOPE_CRITERIA,
    ScopeCriterion,
    ScopeCriterionProposal,
    ScopeDefinition,
    criterion_id,
)
from pydantic import ValidationError

COMMIT = 'a'*40
TEXT = 'Documented development operation succeeds in the clean consumer with observable output. ' * 6


def criteria(count):
    proposals = [ScopeCriterionProposal(description=f'Obligation {i}: {TEXT}', sources=['guide'], applicability='required',
                 rationale=TEXT, usability=TEXT, runtime_needs=[]) for i in range(count)]
    return [ScopeCriterion(**p.model_dump(), id=criterion_id(p)) for p in proposals]


def definition(count):
    return ScopeDefinition(source_commit=COMMIT, inventory_digest='b'*64, inputs={'guide': 'c'*64}, criteria=criteria(count))


def test_scope_accepts_512_criteria_and_rejects_513():
    assert MAX_SCOPE_CRITERIA == 512
    assert len(definition(512).criteria) == 512
    with pytest.raises(ValidationError):
        definition(513)


def test_ledger_accepts_128_findings():
    from coordinare.services.environment_findings import MAX_FINDINGS
    assert MAX_FINDINGS == 128
    records = [FindingRecord(id=f'F{i:04}', manifest_digest='d'*64, source_commit=COMMIT, inputs={'guide': 'c'*64},
               proposal={'category': 'coverage', 'issue': TEXT, 'sources': ['guide'], 'affected_ids': []})
               for i in range(1, 129)]
    ledger = FindingLedger(source_commit=COMMIT, records=records, reviews=[FindingReview(
        sequence=1, manifest_digest='d'*64, inputs={'guide': 'c'*64}, decisions=[], added_ids=[r.id for r in records])])
    assert len(ledger.records) == 128


def test_large_definition_stays_within_its_byte_bound():
    size = len(json.dumps(definition(512).model_dump(mode='json')).encode())
    assert 256*1024 < size <= 1024*1024


def test_document_and_journal_bounds_scale():
    from coordinare_service_inference.environment.models import MAX_DOCUMENT_BYTES

    from coordinare.services.environment_repair import JOURNAL_MAX_BYTES
    assert MAX_DOCUMENT_BYTES == 4*1024*1024 and JOURNAL_MAX_BYTES == 16*1024*1024


def test_assessments_can_decide_every_retained_finding():
    from coordinare_service_inference.environment.models import CoverageAssessmentProposal
    from coordinare_service_inference.environment.scope import ScopeAssessmentProposal
    for model in (ScopeAssessmentProposal, CoverageAssessmentProposal):
        assert model.model_fields['decisions'].metadata[-1].max_length == 128
