"""Scope-only requests retain exact operation, origin and pinned source identity."""
from __future__ import annotations

import copy

import pytest
from coordinare_service_inference.environment.findings import FindingProposal
from pydantic import TypeAdapter, ValidationError

from tests.unit.test_353_scope_contract import COMMIT


def finding(requests):
    return {'category': 'coverage', 'issue': 'Inspect unfamiliar nested obligation',
            'sources': ['guide'], 'affected_ids': [], 'inspection_requests': requests}


def binding_data():
    return {'finding_id': 'F0001', 'review_sequence': 1, 'scope_digest': '1'*64,
            'source_commit': COMMIT, 'inventory_digest': '2'*64,
            'request': {'operation': 'read_file', 'path': 'unfamiliar/entry'}}


def test_rejects_ninth_request_and_exact_duplicates():
    from coordinare_service_inference.environment.scope import ScopeFindingProposal

    requests = [{'operation': 'read_file', 'path': f'unfamiliar/{i}'} for i in range(8)]
    assert len(ScopeFindingProposal.model_validate(finding(requests)).inspection_requests) == 8
    for invalid in [[*requests, {'operation': 'read_file', 'path': 'ninth'}], [*requests, requests[0]], [requests[0]]*2]:
        with pytest.raises(ValidationError):
            ScopeFindingProposal.model_validate(finding(invalid))
    assert ScopeFindingProposal.model_validate(finding([])).inspection_requests == []
    missing = finding([])
    del missing['inspection_requests']
    with pytest.raises(ValidationError):
        ScopeFindingProposal.model_validate(missing)


def test_scope_requests_do_not_change_executable_assessment_schema():
    from coordinare_service_inference.environment.scope import ScopeFindingProposal

    value = finding([{'operation': 'read_file', 'path': 'unfamiliar/entry'}])
    ScopeFindingProposal.model_validate(value)
    with pytest.raises(ValidationError):
        FindingProposal.model_validate(value)
    assert 'inspection_requests' not in FindingProposal.model_json_schema()['properties']


@pytest.mark.parametrize('value', [
    {'operation': 'read_file', 'path': '../escape'},
    {'operation': 'read_file', 'path': 'https://example.invalid/file'},
    {'operation': 'read_file', 'path': ''},
    {'operation': 'list_files', 'path': '.', 'cursor': 0},
    {'operation': 'list_files', 'path': '', 'cursor': -1},
    {'operation': 'list_files', 'path': '', 'cursor': True},
    {'operation': 'list_files', 'path': '', 'cursor': '0'},
    {'operation': 'read_file', 'path': 'guide', 'cursor': 0},
    {'operation': 'execute', 'path': 'guide'},
])
def test_invalid_request_never_admitted(value):
    from coordinare_service_inference.environment.scope_investigation import ScopeInspectionRequest

    with pytest.raises(ValidationError):
        TypeAdapter(ScopeInspectionRequest).validate_python(value)


def test_exact_operation_and_cursor_remain_distinct():
    from coordinare_service_inference.environment.scope import ScopeFindingProposal

    requests = [{'operation': 'read_file', 'path': 'odd'},
                {'operation': 'list_files', 'path': 'odd', 'cursor': 0},
                {'operation': 'list_files', 'path': 'odd', 'cursor': 1},
                {'operation': 'list_files', 'path': '', 'cursor': 0}]
    assert len(ScopeFindingProposal.model_validate(finding(requests)).inspection_requests) == 4


def test_binding_changes_for_each_identity_component():
    from coordinare_service_inference.environment.scope_investigation import ScopeInspectionBinding

    original = binding_data()
    baseline = ScopeInspectionBinding.bind(**original)
    assert len(baseline.id) == 65 and baseline.id.startswith('I')
    for key, value in [('finding_id', 'F0002'), ('review_sequence', 2), ('scope_digest', '3'*64),
                       ('source_commit', 'b'*40), ('inventory_digest', '4'*64),
                       ('request', {'operation': 'read_file', 'path': 'other'})]:
        changed = copy.deepcopy(original)
        changed[key] = value
        assert ScopeInspectionBinding.bind(**changed).id != baseline.id
    bad = baseline.model_dump(mode='json')
    bad['request']['path'] = 'other'
    with pytest.raises(ValidationError):
        ScopeInspectionBinding.model_validate(bad)


@pytest.mark.parametrize('status', ['accept', 'resolved'])
def test_requests_cannot_accompany_acceptance_or_resolution(status):
    from coordinare_service_inference.environment.scope import (
        ScopeAssessmentProposal,
        ScopeFindingDecisionProposal,
    )

    request = {'operation': 'read_file', 'path': 'other'}
    if status == 'resolved':
        value = {'finding_id': 'F0001', 'status': status, 'rationale': 'Resolved', 'sources': ['guide'],
                 'duplicate_of': None, 'inspection_requests': [request]}
        with pytest.raises(ValidationError):
            ScopeFindingDecisionProposal.model_validate(value)
    else:
        with pytest.raises(ValidationError):
            ScopeAssessmentProposal.model_validate({'verdict': status, 'rationale': 'Accepted', 'sources': ['guide'],
                'findings': [finding([request])], 'decisions': []})


@pytest.mark.asyncio
async def test_admitted_request_is_bound_to_daemon_finding_and_review_atomically():
    from coordinare.services.environment_attempt import AttemptBudget
    from tests.unit.test_353_scope_contract import Replies, criterion, proposal, review, run

    class Checkpoint(BaseException):
        pass

    budget = AttemptBudget()
    async def checkpoint():
        if budget.scope_findings and budget.scope_findings.reviews:
            assert budget.scope_findings.open_ids() == ['F0001']
            assert budget.coverage_revisions == 1
            assert len(budget.scope_investigations) == 1
            bound = budget.scope_investigations[0]
            assert bound.finding_id == 'F0001' and bound.review_sequence == 1
            assert bound.scope_digest == budget.scope_definition.digest()
            assert bound.source_commit == COMMIT
            assert bound.inventory_digest == budget.scope_definition.inventory_digest
            assert bound.request.path == 'unfamiliar/entry'
            assert 'inspection_requests' not in budget.scope_findings.records[0].proposal.model_dump()
            raise Checkpoint
    budget.checkpoint = checkpoint
    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()),
        review('revise', findings=[finding([{'operation': 'read_file', 'path': 'unfamiliar/entry'}])])])
    with pytest.raises(Checkpoint):
        await run(replies, budget=budget)


@pytest.mark.asyncio
async def test_queue_overflow_publishes_neither_ledger_nor_partial_bindings():
    from coordinare_service_inference.environment.findings import FindingLedger
    from coordinare_service_inference.environment.scope_investigation import ScopeInspectionBinding

    from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_inspection import SourceInspection
    from coordinare.services.environment_scope import _validate_review
    from tests.unit.test_353_scope_contract import (
        SOURCES,
        Replies,
        criterion,
        proposal,
        review,
        run,
    )

    scope = await run(Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), review()]))
    budget = AttemptBudget(scope_findings=FindingLedger(source_commit=COMMIT))
    for i in range(256):
        value = binding_data()
        value['request']['path'] = f'prior/{i}'
        budget.scope_investigations.append(ScopeInspectionBinding.bind(**value))
    retained = list(budget.scope_investigations)
    async def fetch(path):
        return SOURCES.get(path)
    reviewer = SourceInspection(list(SOURCES), fetch, DiscoveryLimits(), budget)
    await reviewer.read_files(['guide'])
    candidate = review('revise', findings=[finding([{'operation': 'read_file', 'path': 'unfamiliar/entry'}])])
    with pytest.raises(EnvironmentDiscoveryError) as held:
        _validate_review(candidate, reviewer=reviewer, definition=scope.definition,
                         ledger=budget.scope_findings, commit=COMMIT)
    assert held.value.code == 'scope_investigation_limit'
    assert budget.scope_findings.records == [] and budget.scope_findings.reviews == []
    assert budget.scope_investigations == retained
