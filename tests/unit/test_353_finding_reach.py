"""A coverage finding reaches every check group whose records it names, and a group's review decides for its share only."""
from __future__ import annotations

from types import SimpleNamespace

from coordinare_service_inference.environment.models import CoverageAssessmentProposal

from coordinare.services.environment_shard_construction import (
    GLOBAL_OWNER,
    CheckGroup,
    DividedConstruction,
    combine_review,
    finding_parts,
    review_owners,
)
from tests.unit.test_353_sharded_executable import make_criterion


def groups() -> list[CheckGroup]:
    return [CheckGroup('c00-', (make_criterion(1),)), CheckGroup('c01-', (make_criterion(2),))]


def record(identifier: str, *affected: str) -> SimpleNamespace:
    proposal = SimpleNamespace(affected_ids=list(affected), category='execution_definition')
    return SimpleNamespace(id=identifier, proposal=proposal, model_dump=lambda mode='json': {'id': identifier})


def test_a_finding_naming_two_groups_reaches_both():
    assert finding_parts(['c00-0', 'c01-0'], groups()) == ['c00-', 'c01-']
    assert finding_parts(['c00-0', 'install-node'], groups()) == ['c00-', GLOBAL_OWNER]
    assert finding_parts([], groups()) == [GLOBAL_OWNER]


def test_route_findings_delivers_a_cross_group_finding_to_each_named_group():
    ledger = SimpleNamespace(records=[record('F0001', 'c00-0', 'c01-0')], open_ids=lambda: ['F0001'])
    build = SimpleNamespace(groups=groups(), feedback={})
    DividedConstruction.route_findings(build, ledger)
    assert set(build.feedback) == {'c00-', 'c01-'}
    assert build.feedback['c01-']['open_findings'] == [{'id': 'F0001'}]


def test_review_assigns_a_cross_group_finding_to_each_named_group():
    ledger = SimpleNamespace(records=[record('F0001', 'c00-0', 'c01-0'), record('F0002', 'c01-1')])
    assert review_owners(ledger, groups()) == {'c00-': ['F0001'], 'c01-': ['F0001', 'F0002']}


def assessment(verdict: str, *decisions: tuple[str, str]) -> CoverageAssessmentProposal:
    return CoverageAssessmentProposal.model_validate({
        'findings': [], 'capability_unavailable': False, 'verdict': verdict, 'rationale': 'judged', 'sources': ['a.md'],
        'decisions': [{'finding_id': fid, 'status': status, 'rationale': 'judged', 'sources': ['a.md'], 'duplicate_of': None}
                      for fid, status in decisions]})


def test_a_shared_finding_stays_open_until_every_group_resolves_it():
    ledger = SimpleNamespace(records=[])
    one = assessment('accept', ('F0001', 'resolved'))
    other_open = assessment('revise', ('F0001', 'open'))
    merged = combine_review([('c00-', one), ('c01-', other_open)], ledger)
    assert [(d['finding_id'], d['status']) for d in merged['decisions']] == [('F0001', 'open')]
    merged = combine_review([('c00-', one), ('c01-', assessment('accept', ('F0001', 'resolved')))], ledger)
    assert [(d['finding_id'], d['status']) for d in merged['decisions']] == [('F0001', 'resolved')]
