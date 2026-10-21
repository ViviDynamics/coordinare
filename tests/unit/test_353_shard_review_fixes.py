"""Sharded discovery keeps every routed reference, every reviewed byte and every response binding."""
from __future__ import annotations

import json

import pytest
from coordinare_service_inference.environment.findings import FindingResponse
from coordinare_service_inference.environment.inventory import InventoryEntry
from coordinare_service_inference.environment.scope import (
    ScopeCriterion,
    ScopeCriterionProposal,
    ScopeDefinition,
    criterion_id,
)
from coordinare_service_inference.environment.scope_revision import ScopeReassessment, ScopeRevision

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_shard_scope import (
    MERGE_OWNER,
    ReviewMaterial,
    ShardedScope,
    _fingerprints,
    build_plan,
    combine_revisions,
)
from coordinare.services.environment_shards import plan_shards
from tests.unit.test_353_shard_scope import (
    COMMIT,
    ENTRIES,
    LIMITS,
    PLAN,
    SOURCES,
    Router,
    criterion,
    run,
)


def full(path, label, **changes):
    proposal = ScopeCriterionProposal(**{**criterion(path, label), **changes})
    return ScopeCriterion(**proposal.model_dump(), id=criterion_id(proposal))


def definition(*criteria):
    paths = {path for c in criteria for path in c.sources}
    return ScopeDefinition(source_commit=COMMIT, inventory_digest='d' * 64, inputs=dict.fromkeys(paths, 'a' * 64),
                           criteria=list(criteria))


CHAIN = ['svc0/file1.txt', 'svc1/file1.txt', 'svc2/file1.txt', 'svc0/file2.txt', 'svc1/file2.txt']


class ChainRouter(Router):
    """Triage names only each service's first file; references chain across shards for four follow-up hops."""

    def __init__(self):
        super().__init__()
        self.hop = 0

    def triage(self, messages, user):
        tops = sorted({item['path'].split('/')[0] for item in user['files']} - {'README.md'})
        verdicts = [{'path': top, 'verdict': 'unsure', 'rationale': 'r'} for top in tops]
        verdicts += [{'path': item['path'], 'verdict': 'environment', 'rationale': 'r'} for item in user['files']
                     if item['path'].endswith('file0.txt') or item['path'] == 'README.md']
        return {'listing_triage': {'verdicts': verdicts}}

    def author(self, messages, user):
        follow = '.r' in user['shard']['id']
        if len(messages) == 2:
            if follow:
                self.hop += 1
                return {'tool': 'read_file', 'path': CHAIN[self.hop - 1]}
            return {'tool': 'read_file', 'path': user['shard_files'][0]['path']}
        read = json.loads(messages[-1]['content'])['tool_result']['path']
        nxt = CHAIN[CHAIN.index(read) + 1] if read in CHAIN[:-1] else None
        start = read == 'svc0/file0.txt'
        return {'shard_summary': {'criteria': [criterion(read, read)],
                                  'cross_references': [CHAIN[0]] if start else [nxt] if nxt else [], 'questions': []}}


@pytest.mark.asyncio
async def test_references_are_followed_through_every_hop():
    contract = await run(ChainRouter())
    assert set(CHAIN) <= set(contract.definition.inputs)
    assert {c.sources[0] for c in contract.definition.criteria} >= set(CHAIN)


def test_rationale_only_change_is_a_different_review_subject():
    before = full('svc0/file0.txt', 'x')
    after = full('svc0/file0.txt', 'x', rationale='A different cited rationale')
    assert before.id == after.id and _fingerprints([before]) != _fingerprints([after])


@pytest.mark.asyncio
async def test_merge_reviewer_judges_root_criteria_in_full():
    router = Router()
    await run(router)
    [merge] = [json.loads(m[1]['content']) for role, m in router.conversations if role == 'merge']
    root = [c for c in merge['scope_criteria'] if c['sources'] == ['README.md']]
    assert root and root[0]['rationale'] == 'Cited source obligation' and root[0]['usability']
    assert all('sources' not in entry for entry in merge['criterion_index'])


class InvestigatingRouter(Router):
    """svc1's reviewer asks for an unread file in svc2; svc1's next reviewer receives the investigation."""

    TARGET = 'svc2/file2.txt'

    def review(self, messages, user):
        shard_files = [item['path'] for item in user['mandatory_evidence'] if item['path'] not in self.plan.spine]
        if shard_files and shard_files[0].startswith('svc1') and not user['required_decision_ids']:
            finding = {'category': 'coverage', 'issue': 'svc1 depends on an unread svc2 file', 'sources': [shard_files[0]],
                       'affected_ids': [], 'gap_kind': 'unread_source',
                       'inspection_requests': [{'operation': 'read_file', 'path': self.TARGET}]}
            return {'scope_assessment': {'verdict': 'revise', 'rationale': 'Gap', 'sources': [shard_files[0]],
                                         'findings': [finding], 'decisions': []}}
        return super().review(messages, user)

    def revision(self, messages, user):
        reply = super().revision(messages, user)
        for response in reply['finding_responses']:
            response['sources'].append(self.TARGET)
        return reply


@pytest.mark.asyncio
async def test_reviewers_receive_their_findings_investigations():
    entries = {**ENTRIES}
    router = InvestigatingRouter()
    await run(router, entries=entries)
    later = [json.loads(m[1]['content']) for role, m in router.conversations if role == 'review']
    investigated = [user for user in later if user['investigation_evidence']]
    assert investigated and investigated[0]['investigation_evidence'][0]['binding']['request']['path'] == InvestigatingRouter.TARGET


def test_shards_stay_within_the_per_conversation_read_allowance():
    entries = {f'pkg/m{i:03}.py': InventoryEntry(f'pkg/m{i:03}.py', 100, '100644') for i in range(300)}
    shards = plan_shards(entries, share=10_000_000, spine=[], max_file_bytes=4096, max_files=100)
    assert all(len(shard.paths) <= 100 for shard in shards) and sum(len(s.paths) for s in shards) == 300
    plan = build_plan(entries, context_tokens=4_000_000, max_file_bytes=4096, max_reads=256)
    assert all(len(shard.paths) <= 128 for shard in plan.shards)


def test_a_part_reports_at_most_its_share_of_finding_capacity():
    from coordinare.services.environment_scope import _Request, _ResponseReferenceError

    async def fetch(path):
        return SOURCES.get(path)
    budget = AttemptBudget()
    from coordinare_service_inference.environment.findings import FindingLedger
    budget.scope_findings = FindingLedger(source_commit=COMMIT)
    scope = ShardedScope(_Request(list(SOURCES), fetch, None, COMMIT, LIMITS, budget, entries=ENTRIES), ENTRIES, PLAN, 4)
    inspection = scope.view(name='scope_shard_review')
    inspection.admit_content('README.md', SOURCES['README.md'], fetched=False)
    item = full('README.md', 'x')
    data = {'scope_assessment': {'verdict': 'revise', 'rationale': 'r', 'sources': ['README.md'], 'decisions': [],
        'findings': [{'category': 'coverage', 'issue': 'gap', 'sources': ['README.md'], 'affected_ids': [],
                      'gap_kind': 'missing_obligation', 'inspection_requests': []}]}}
    with pytest.raises(_ResponseReferenceError) as failure:
        scope._validate_part(data, inspection=inspection, definition=definition(item),
                             material=ReviewMaterial([], [], [], 0))
    assert failure.value.errors[0]['code'] == 'review_findings_limit'


def test_revisions_combine_duplicates_and_follow_reassessed_ids():
    unknown = full('svc0/file0.txt', 'u', applicability='unknown')
    base = definition(unknown, full('svc1/file0.txt', 'kept'))
    added = ScopeCriterionProposal(**criterion('svc2/file0.txt', 'shared addition'))
    known = ScopeCriterionProposal(**criterion('svc0/file0.txt', 'u'))
    first = ScopeRevision(base_digest=base.digest(), add=[added], reassess=[ScopeReassessment(criterion_id=unknown.id, criterion=known)])
    second = ScopeRevision(base_digest=base.digest(), add=[added], reassess=[])
    response = FindingResponse(finding_id='F0001', rationale='r', sources=['svc1/file0.txt'], affected_ids=[unknown.id])
    inputs = {**base.inputs, 'svc2/file0.txt': 'a' * 64}
    combined, responses = combine_revisions(base, [(first, []), (second, [response])], inputs=inputs)
    assert sum(c.description == 'Obligation shared addition' for c in combined.criteria) == 1
    assert responses[0].affected_ids == [criterion_id(known)]


def test_sharded_discovery_gets_the_ruled_budget_unless_set():
    from coordinare.config import EnvCacheConfig
    sharded = EnvCacheConfig(manifest_shard_context_tokens=262144)
    assert (sharded.manifest_max_model_calls, sharded.manifest_max_total_reads,
            sharded.manifest_attempt_timeout_seconds) == (1024, 8192, 14400.0)
    explicit = EnvCacheConfig(manifest_shard_context_tokens=262144, manifest_max_model_calls=64)
    assert explicit.manifest_max_model_calls == 64
    assert EnvCacheConfig().manifest_max_model_calls == 128


@pytest.mark.asyncio
async def test_an_accepted_sharded_scope_resumes_beyond_the_conversation_read_allowance(tmp_path):
    from coordinare.services.environment_repair import begin_attempt, save_attempt
    from coordinare.services.environment_shard_scope import discover_source_scope
    from tests.unit.test_353_execution_repair import cache_fixture

    limits = DiscoveryLimits(max_file_bytes=4096, shard_context_tokens=4000, agent_concurrency=4, max_reads=4)
    cache = cache_fixture(tmp_path)
    cache.bootstrap_commit_sha = COMMIT

    async def fetch(path):
        return SOURCES.get(path)

    async def attempt(router):
        budget = begin_attempt(cache, limits, outer_seconds=600)

        async def checkpoint():
            save_attempt(cache, budget)
        budget.checkpoint = checkpoint
        return await discover_source_scope(paths=list(SOURCES), fetch=fetch, chat=router, commit=COMMIT,
                                           limits=limits, budget=budget, entries=ENTRIES)

    plan = build_plan(ENTRIES, context_tokens=4000, max_file_bytes=4096, max_reads=4)
    accepted = await attempt(Router(plan=plan))
    assert len(accepted.all_inputs) > limits.max_reads
    spent = cache.environment_attempt['reads']
    resumed = await attempt(Router(plan=plan))
    assert resumed == accepted
    # Resume verification is integrity work; it does not spend the attempt's model read allowance.
    assert cache.environment_attempt['reads'] == spent


def test_a_readable_file_larger_than_one_share_gets_its_own_shard_never_silence():
    entries = {'big.txt': InventoryEntry('big.txt', 4000, '100644'), 'ok.txt': InventoryEntry('ok.txt', 10, '100644')}
    # Its agent may overflow and hold loudly; it is never silently left out of every shard.
    assert sorted(shard.paths for shard in plan_shards(entries, share=1000, spine=[], max_file_bytes=4096)) == [
        ('big.txt',), ('ok.txt',)]


def test_finding_capacity_is_split_exactly():
    from coordinare.services.environment_shard_scope import finding_capacities
    assert finding_capacities(10, 13) == [1] * 10 + [0] * 3
    assert sum(finding_capacities(128, 20)) == 128 and max(finding_capacities(128, 20)) - min(finding_capacities(128, 20)) <= 1


@pytest.mark.asyncio
async def test_a_spent_read_allowance_is_reported_to_the_agent_not_held():
    from coordinare.services.environment_inspection import SourceInspection
    from coordinare.services.environment_shard_scope import _bounded_tool_result

    async def fetch(path):
        return SOURCES.get(path)
    inspection = SourceInspection(ENTRIES, fetch, DiscoveryLimits(max_reads=1), AttemptBudget())
    await inspection.read_files(['README.md'])
    result = await _bounded_tool_result(inspection, {'tool': 'read_file', 'path': 'svc0/file0.txt'})
    assert result['errors'][0]['code'] == 'read_allowance_exhausted'


def test_combined_revisions_beyond_the_criterion_cap_hold_as_size_limit(monkeypatch):
    import coordinare.services.environment_shard_scope as sharded
    monkeypatch.setattr(sharded, 'MAX_SCOPE_CRITERIA', 2)
    base = definition(full('svc1/file0.txt', 'kept'))
    adds = [ScopeRevision(base_digest=base.digest(), add=[ScopeCriterionProposal(**criterion('svc1/file0.txt', f'new {i}'))],
                          reassess=[]) for i in range(2)]
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        combine_revisions(base, [(item, []) for item in adds], inputs=base.inputs)
    assert failure.value.code == 'scope_size_limit'


def test_identical_reassessments_from_two_groups_are_one_change():
    unknown = full('svc0/file0.txt', 'u', applicability='unknown')
    base = definition(unknown)
    known = ScopeCriterionProposal(**criterion('svc0/file0.txt', 'u'))
    same = ScopeRevision(base_digest=base.digest(), add=[], reassess=[ScopeReassessment(criterion_id=unknown.id, criterion=known)])
    combined, _ = combine_revisions(base, [(same, []), (same, [])], inputs=base.inputs)
    assert [c.id for c in combined.criteria] == [criterion_id(known)]


_ = MERGE_OWNER
