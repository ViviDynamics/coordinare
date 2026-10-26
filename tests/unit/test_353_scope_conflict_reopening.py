"""Current independent source conflicts authorize exact, temporary criterion corrections."""
from __future__ import annotations

import json

import pytest
from coordinare_service_inference.environment.findings import FindingLedger
from coordinare_service_inference.environment.inventory import InventoryEntry
from coordinare_service_inference.environment.scope import ScopeDefinition

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_inspection import SourceInspection
from coordinare.services.environment_scope import _Request, _validate_review, _validate_revision
from tests.unit.test_353_shard_review_fixes import full

COMMIT = 'a' * 40
FILES = {'nested/operations': 'Use widget scan for the local verification.',
         'nested/retirement': 'The old widget rebuild procedure has been removed.',
         'new/retirement': 'A newer source correction identifies the separate retired procedure.',
         'other/operations': 'The separate storage verification remains required.'}
ENTRIES = {path: InventoryEntry(path, len(content.encode()), '100644') for path, content in FILES.items()}


async def fetch(path):
    return FILES.get(path)


def fixture(*, retained=False, kind='source_conflict', budget=None):
    budget = budget or AttemptBudget()
    reviewer = SourceInspection(ENTRIES, fetch, DiscoveryLimits(), budget)
    for path, content in FILES.items():
        reviewer.admit_content(path, content, fetched=False)
    target = full('nested/operations', 'Perform the superseded widget rebuild')
    protected = full('other/operations', 'Verify the independent storage operation')
    base = ScopeDefinition(source_commit=COMMIT, inventory_digest=reviewer.inventory_digest,
                           inputs=dict(reviewer.inputs), criteria=[target, protected])
    data = {'scope_assessment': {'verdict': 'revise', 'rationale': 'Pinned retirement evidence contradicts the draft',
        'sources': ['nested/retirement'], 'findings': [{'category': 'coverage', 'issue': 'Superseded procedure',
        'sources': ['nested/retirement'], 'affected_ids': [target.id], 'gap_kind': kind, 'inspection_requests': []}],
        'decisions': []}}
    _, ledger, record = _validate_review(data, reviewer=reviewer, definition=base,
                                        ledger=FindingLedger(source_commit=COMMIT), commit=COMMIT)
    if retained:
        data['scope_assessment']['findings'] = []
        data['scope_assessment']['decisions'] = [{'finding_id': 'F0001', 'disposition': 'source_gap',
            'rationale': 'The pinned conflict remains', 'sources': ['nested/retirement'],
            'affected_ids': [target.id], 'gap_kind': kind, 'inspection_requests': []}]
        _, ledger, record = _validate_review(data, reviewer=reviewer, definition=base, ledger=ledger, commit=COMMIT)
    budget.scope_definition = base
    budget.scope_inputs = dict(reviewer.inputs)
    budget.scope_findings = ledger
    budget.scope_assessment = record
    budget.scope_phase = 'revision'
    budget.scope_revision_base_digest = base.digest()
    return budget, base, target, protected, reviewer


def revision(base, target, applicability='not_applicable'):
    replacement = target.model_dump(exclude={'id'}) | {
        'description': 'The current widget scan is used' if applicability == 'required' else 'The retired rebuild is excluded',
        'rationale': 'Pinned retirement guidance corrects the draft requirement',
        'sources': ['nested/operations', 'nested/retirement'], 'applicability': applicability,
    }
    return {'scope_revision': {'base_digest': base.digest(), 'add': [],
        'reassess': [{'criterion_id': target.id, 'criterion': replacement}]},
        'finding_responses': [{'finding_id': 'F0001', 'rationale': 'Corrected using the retirement evidence',
                               'sources': ['nested/retirement'], 'affected_ids': []}]}


@pytest.mark.parametrize('retained', [False, True])
@pytest.mark.parametrize('applicability', ['required', 'not_applicable'])
def test_current_independent_conflict_allows_only_its_named_required_reassessment(retained, applicability):
    budget, base, target, protected, author = fixture(retained=retained)
    history = budget.scope_findings.canonical()
    corrected = _validate_revision(revision(base, target, applicability), author=author, commit=COMMIT, budget=budget)
    assert corrected.criteria[0].applicability == applicability and corrected.criteria[0].id != target.id
    assert corrected.criteria[1] == protected
    assert base.criteria[0] == target and budget.scope_findings.canonical() == history
    assert budget.scope_findings.open_ids() == ['F0001'] and budget.scope_contract is None
    with pytest.raises(EnvironmentDiscoveryError):
        _validate_revision(revision(base, protected), author=author, commit=COMMIT, budget=budget)


@pytest.mark.asyncio
@pytest.mark.parametrize('retained', [False, True])
@pytest.mark.parametrize('applicability', ['required', 'not_applicable'])
async def test_sharded_cross_owner_conflict_is_corrected_then_independently_reviewed(retained, applicability):
    from coordinare.services.environment_shard_scope import ShardedScope, ShardPlan, build_plan
    from coordinare.services.environment_shards import Shard

    budget, base, target, protected, _ = fixture(retained=retained)

    async def chat(messages, *, response_schema):
        data = json.loads(messages[1]['content'])
        assert data['reopened_criterion_ids'] == [target.id]
        assert any(c['id'] == target.id for c in data['scope_criteria'])
        return json.dumps(revision(base, target, applicability))

    plan = build_plan(ENTRIES, context_tokens=4096, max_file_bytes=4096)
    scope = ShardedScope(_Request(list(FILES), fetch, chat, COMMIT, DiscoveryLimits(), budget, entries=ENTRIES),
                         ENTRIES, plan, 2)
    scope._work = ShardPlan((Shard('first', ('nested/operations',), 64, 0),
                            Shard('second', ('nested/retirement', 'other/operations'), 128, 0)), (), 8192,
                           {'nested/operations': 'first', 'nested/retirement': 'second', 'other/operations': 'second'})
    corrected = await scope.revision_round(base)
    assert corrected.criteria[0].applicability == applicability and corrected.criteria[1] == protected
    assert budget.scope_phase == 'review' and budget.scope_contract is None
    assert budget.scope_findings.open_ids() == ['F0001'] and budget.probe_batches == 0
    reviewer = scope.view(name='scope_review')
    for path, content in FILES.items():
        reviewer.admit_content(path, content, fetched=False)
    _, reviewed, _ = _validate_review({'scope_assessment': {'verdict': 'accept', 'rationale': 'Fresh evidence review',
        'sources': list(FILES), 'findings': [], 'decisions': [{'finding_id': 'F0001', 'disposition': 'source_correction',
        'rationale': 'The corrected criterion matches the pinned sources', 'sources': list(FILES)}]}},
        reviewer=reviewer, definition=corrected, ledger=budget.scope_findings, commit=COMMIT)
    assert reviewed.open_ids() == [] and len(reviewed.reviews) == len(budget.scope_findings.reviews) + 1


@pytest.mark.parametrize('case', ['missing_assessment', 'nonconflict', 'superseded_kind', 'resolved',
                                  'old_sequence', 'wrong_digest', 'wrong_commit', 'source_drift', 'assignment'])
def test_old_or_unbound_conflicts_cannot_authorize_reassessment(case):
    from coordinare.services.environment_scope_reopening import source_conflict_reopenings

    budget, base, target, _, reviewer = fixture()
    if case == 'missing_assessment':
        budget.scope_assessment = None
    elif case in {'superseded_kind', 'resolved', 'old_sequence'}:
        decision = {'finding_id': 'F0001', 'disposition': 'source_correction',
                    'rationale': 'Current source assessment', 'sources': ['nested/retirement']}
        if case != 'resolved':
            decision |= {'disposition': 'source_gap', 'gap_kind': 'missing_obligation',
                         'affected_ids': [target.id], 'inspection_requests': []}
        _, ledger, record = _validate_review({'scope_assessment': {'verdict': 'revise' if case != 'resolved' else 'accept',
            'rationale': 'Current assessment supersedes the first conflict', 'sources': ['nested/retirement'],
            'findings': [], 'decisions': [decision]}}, reviewer=reviewer, definition=base,
            ledger=budget.scope_findings, commit=COMMIT)
        budget.scope_findings = ledger
        if case != 'old_sequence':
            budget.scope_assessment = record
    elif case == 'nonconflict':
        budget, base, target, _, _ = fixture(kind='missing_obligation')
    elif case == 'wrong_digest':
        budget.scope_assessment.scope_digest = '0' * 64
    elif case == 'wrong_commit':
        budget.scope_assessment.source_commit = 'b' * 40
    elif case == 'source_drift':
        budget.scope_assessment.reviewer_inputs = dict(budget.scope_assessment.reviewer_inputs) | {'nested/operations':'f'*64}
    if case in {'old_sequence', 'wrong_digest', 'wrong_commit', 'source_drift'}:
        with pytest.raises(EnvironmentDiscoveryError):
            source_conflict_reopenings(budget, base)
    else:
        assert source_conflict_reopenings(budget, base, finding_ids=[] if case == 'assignment' else None) == set()


def test_restart_retains_only_current_typed_authority_and_original_limits(tmp_path):
    from coordinare.models.env_cache import EnvCacheState
    from coordinare.services.environment_repair import begin_attempt, save_attempt
    from coordinare.services.environment_scope_reopening import source_conflict_reopenings

    cache = EnvCacheState(symphony_name='fixture', sanitised_name='fixture', cache_dir=tmp_path/'cache',
                          bootstrap_commit_sha=COMMIT)
    limits = DiscoveryLimits()
    budget = begin_attempt(cache, limits, outer_seconds=600, now=100)
    budget, base, target, _, _ = fixture(retained=True, budget=budget)
    budget.calls, budget.reads, budget.scope_revisions = 3, 4, 1
    save_attempt(cache, budget)
    before = json.dumps(cache.environment_attempt, sort_keys=True)
    resumed = begin_attempt(cache, limits, outer_seconds=3600, now=120)
    assert source_conflict_reopenings(resumed, resumed.scope_definition) == {target.id}
    assert (resumed.calls, resumed.reads, resumed.scope_revisions) == (3, 4, 1)
    assert cache.environment_attempt['expires_at'] == 700
    assert json.dumps(cache.environment_attempt, sort_keys=True) == before
    assert resumed.scope_contract is None and resumed.scope_definition == base


def test_source_conflict_correction_cannot_demote_a_required_criterion_to_unknown():
    from pydantic_core import PydanticCustomError

    budget, base, target, _, author = fixture()
    with pytest.raises((EnvironmentDiscoveryError, PydanticCustomError)):
        _validate_revision(revision(base, target, 'unknown'), author=author, commit=COMMIT, budget=budget)
    assert budget.scope_findings.open_ids() == ['F0001'] and budget.scope_definition == base


def test_reviewer_must_have_independently_read_the_reopened_criterion_sources():
    from coordinare.services.environment_scope_reopening import source_conflict_reopenings

    budget, base, target, _, _ = fixture()
    reviewer = SourceInspection(ENTRIES, fetch, DiscoveryLimits(), budget)
    reviewer.admit_content('nested/retirement', FILES['nested/retirement'], fetched=False)
    _, ledger, record = _validate_review({'scope_assessment': {'verdict': 'revise',
        'rationale': 'The current review lacks the criterion source', 'sources': ['nested/retirement'],
        'findings': [{'category': 'coverage', 'issue': 'Source conflict', 'sources': ['nested/retirement'],
                     'affected_ids': [target.id], 'gap_kind': 'source_conflict', 'inspection_requests': []}],
        'decisions': []}}, reviewer=reviewer, definition=base,
        ledger=FindingLedger(source_commit=COMMIT), commit=COMMIT)
    budget.scope_findings, budget.scope_assessment = ledger, record
    with pytest.raises(EnvironmentDiscoveryError, match='current independent evidence'):
        source_conflict_reopenings(budget, base)


def test_duplicate_conflict_does_not_retain_its_old_reassessment_authority():
    from coordinare.services.environment_scope_reopening import source_conflict_reopenings

    budget, base, target, protected, reviewer = fixture()
    data = budget.scope_assessment.assessment.model_dump(mode='json')
    data['findings'].append(data['findings'][0] | {'affected_ids': [protected.id], 'issue': 'Second draft conflict'})
    _, ledger, _ = _validate_review({'scope_assessment': data}, reviewer=reviewer, definition=base,
                                   ledger=FindingLedger(source_commit=COMMIT), commit=COMMIT)
    data['findings'] = []
    data['decisions'] = [{'finding_id': 'F0001', 'disposition': 'source_gap', 'gap_kind': 'source_conflict',
        'rationale': 'Only this conflict remains', 'sources': list(FILES), 'affected_ids': [target.id],
        'inspection_requests': []}, {'finding_id': 'F0002', 'disposition': 'duplicate', 'duplicate_of': 'F0001',
        'rationale': 'Consolidated by independent review', 'sources': list(FILES)}]
    _, ledger, record = _validate_review({'scope_assessment': data}, reviewer=reviewer, definition=base,
                                        ledger=ledger, commit=COMMIT)
    budget.scope_findings, budget.scope_assessment = ledger, record
    assert source_conflict_reopenings(budget, base) == {target.id}


def test_full_scope_author_cannot_delete_a_reopened_required_criterion():
    from coordinare.services.environment_scope import _validate_author

    budget, base, _, protected, author = fixture()
    data = {'scope': {'criteria': [protected.model_dump(exclude={'id'})]},
            'finding_responses': [{'finding_id': 'F0001', 'rationale': 'An author cannot remove the obligation',
                                   'sources': ['nested/retirement'], 'affected_ids': []}]}
    with pytest.raises(EnvironmentDiscoveryError, match='scope_obligation_removed'):
        _validate_author(data, author=author, commit=COMMIT, budget=budget)
    assert budget.scope_definition == base and budget.scope_findings.open_ids() == ['F0001']


@pytest.mark.asyncio
@pytest.mark.parametrize('applicability', ['required', 'not_applicable'])
async def test_single_discovery_accepts_correction_only_after_a_fresh_independent_review(applicability):
    from coordinare_service_inference.environment.scope import ScopeCriterionProposal, criterion_id

    from tests.unit.test_353_scope_contract import Replies, criterion, proposal, review, run

    target = criterion(usability='The retired local procedure succeeds')
    protected = criterion('odd/nest', usability='The separate local operation succeeds')
    target_id = criterion_id(ScopeCriterionProposal.model_validate(target))
    protected_id = criterion_id(ScopeCriterionProposal.model_validate(protected))
    corrected = target | {'description': 'Current pinned procedure', 'applicability': applicability,
                          'rationale': 'Pinned source retirement corrects the earlier draft'}
    budget = AttemptBudget()

    def correction(messages):
        context = json.loads(messages[1]['content'])
        assert context['reopened_criterion_ids'] == [target_id]
        assert budget.scope_contract is None and budget.scope_findings.open_ids() == ['F0001']
        return {'scope_revision': {'base_digest': context['scope_base_digest'], 'add': [],
            'reassess': [{'criterion_id': target_id, 'criterion': corrected}]},
            'finding_responses': [{'finding_id': 'F0001', 'rationale': 'Source-backed correction',
                                   'sources': ['guide'], 'affected_ids': []}]}

    def fresh_review(messages):
        assert len(messages) == 2  # The author conversation is never reused for independent acceptance.
        assert budget.scope_contract is None and budget.scope_findings.open_ids() == ['F0001']
        return review(decisions=[{'finding_id': 'F0001', 'disposition': 'source_correction',
            'rationale': 'Fresh inspection confirms the corrected obligation', 'sources': ['guide']}])

    replies = Replies([{'tool': 'read_files', 'paths': ['guide', 'odd/nest']}, proposal(target, protected),
        review('revise', findings=[{'category': 'coverage', 'issue': 'Pinned guidance contradicts the draft',
            'gap_kind': 'source_conflict', 'sources': ['guide'], 'affected_ids': [target_id]}]),
        correction, fresh_review])
    accepted = await run(replies, budget=budget)
    assert accepted.current and accepted.finding_ledger.open_ids() == []
    assert accepted.source_assessment.review_sequence == 2
    assert next(c for c in accepted.definition.criteria if c.id == protected_id).applicability == 'required'
    assert accepted.definition.criteria[0].id != target_id
    assert budget.scope_revisions == 1 and budget.probe_batches == 0 and budget.coverage_manifest is None


@pytest.mark.asyncio
async def test_corrected_scope_resume_waits_for_fresh_review_without_reusing_old_authority(tmp_path):
    from coordinare.models.env_cache import EnvCacheState
    from coordinare.services.environment_repair import begin_attempt, save_attempt
    from coordinare.services.environment_scope import discover_scope
    from tests.unit.test_353_scope_contract import Replies

    cache = EnvCacheState(symphony_name='fixture', sanitised_name='fixture', cache_dir=tmp_path/'cache',
                          bootstrap_commit_sha=COMMIT)
    limits = DiscoveryLimits()
    budget = begin_attempt(cache, limits, outer_seconds=600)
    budget, base, target, _, author = fixture(budget=budget)
    budget.scope_definition = _validate_revision(revision(base, target), author=author, commit=COMMIT, budget=budget)
    budget.scope_phase, budget.scope_revision_base_digest = 'review', None
    save_attempt(cache, budget)
    deadline = cache.environment_attempt['expires_at']
    resumed = begin_attempt(cache, limits, outer_seconds=3600)
    replies = Replies([{'scope_assessment': {'verdict': 'accept', 'rationale': 'Fresh post-restart source review',
        'sources': list(FILES), 'findings': [], 'decisions': [{'finding_id': 'F0001', 'disposition': 'source_correction',
        'rationale': 'The corrected obligation is supported', 'sources': list(FILES)}]}}])
    accepted = await discover_scope(paths=list(FILES), fetch=fetch, chat=replies, commit=COMMIT,
                                    limits=limits, budget=resumed, entries=ENTRIES)
    assert accepted.current and len(replies.messages) == 1
    assert resumed.scope_phase == 'accepted' and cache.environment_attempt['expires_at'] == deadline


@pytest.mark.asyncio
async def test_retained_conflict_author_receives_current_target_and_new_evidence():
    from coordinare.services.environment_shard_scope import ShardedScope, build_plan

    budget, base, target, protected, reviewer = fixture()
    data = {'scope_assessment': {'verdict': 'revise', 'rationale': 'New evidence corrects the affected target',
        'sources': ['new/retirement'], 'findings': [], 'decisions': [{'finding_id': 'F0001',
        'disposition': 'source_gap', 'gap_kind': 'source_conflict', 'affected_ids': [protected.id],
        'sources': ['new/retirement'], 'rationale': 'The current conflict concerns the separate procedure',
        'inspection_requests': []}]}}
    _, ledger, record = _validate_review(data, reviewer=reviewer, definition=base,
                                        ledger=budget.scope_findings, commit=COMMIT)
    budget.scope_findings, budget.scope_assessment = ledger, record

    async def chat(messages, *, response_schema):
        context = json.loads(messages[1]['content'])
        assert context['reopened_criterion_ids'] == [protected.id]
        current = context['current_source_conflicts']['F0001']
        assert current['affected_ids'] == [protected.id] and current['sources'] == ['new/retirement']
        assert any(item['path'] == 'new/retirement' for item in context['mandatory_evidence'])
        response = revision(base, protected)
        response['finding_responses'][0]['sources'] = ['new/retirement']
        return json.dumps(response)

    plan = build_plan(ENTRIES, context_tokens=4096, max_file_bytes=4096)
    scope = ShardedScope(_Request(list(FILES), fetch, chat, COMMIT, DiscoveryLimits(), budget, entries=ENTRIES),
                         ENTRIES, plan, 2)
    corrected = await scope.revision_round(base)
    assert corrected.criteria[0] == target and corrected.criteria[1].id != protected.id
    assert budget.scope_findings.open_ids() == ['F0001'] and budget.scope_phase == 'review'
