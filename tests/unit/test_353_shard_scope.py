"""Sharded source discovery: shard authors and independent shard reviewers feed one definition and one ledger."""
from __future__ import annotations

import copy
import json

import pytest
from coordinare_service_inference.environment.inventory import InventoryEntry
from coordinare_service_inference.environment.scope import (
    ScopeAssessmentProposal,
    ScopeCriterionProposal,
    ShardSummary,
)

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_shard_scope import (
    MERGE_OWNER,
    MERGE_REVIEW_PREAMBLE,
    SHARD_AUTHOR_INSTRUCTIONS,
    SHARD_REVIEW_PREAMBLE,
    SHARD_REVISION_PREAMBLE,
    build_plan,
    combine_round,
    merge_summaries,
    needs_sharding,
    route_cross_references,
)

COMMIT = 'a' * 40
TOKENS = 4000  # share = 7,000 bytes
LIMITS = DiscoveryLimits(max_file_bytes=4096, shard_context_tokens=TOKENS, agent_concurrency=4)
SOURCES = {'README.md': 'Root guide: run make test in each service.',
           **{f'svc{d}/file{i}.txt': f'Service {d} file {i}: ' + 'x' * 1500 for d in range(3) for i in range(3)}}
ENTRIES = {path: InventoryEntry(path, len(text.encode()), '100644') for path, text in SOURCES.items()}
PLAN = build_plan(ENTRIES, context_tokens=TOKENS, max_file_bytes=LIMITS.max_file_bytes)
AUTHOR_ONLY = 'AUTHOR-ONLY-QUESTION'


def criterion(path, label):
    return {'description': f'Obligation {label}', 'sources': [path], 'applicability': 'required',
            'rationale': 'Cited source obligation', 'usability': f'{label} succeeds', 'runtime_needs': []}


def decision(finding_id, criterion_ids):
    return {'finding_id': finding_id, 'rationale': 'Covered by current criteria', 'sources': ['README.md'],
            'disposition': 'covered', 'criterion_ids': criterion_ids, 'gap_kind': None, 'affected_ids': [],
            'inspection_requests': [], 'duplicate_of': None}


class Router:
    """Answers each agent conversation by its role, recording every conversation it saw."""

    def __init__(self, *, revise_shard=None, invalid_author=0):
        self.revise_shard = revise_shard
        self.invalid_author = invalid_author
        self.conversations: list[tuple[str, list[dict[str, str]]]] = []
        self.reviews = 0

    async def __call__(self, messages, *, response_schema):
        system, user = messages[0]['content'], json.loads(messages[1]['content'])
        role = ('author' if SHARD_AUTHOR_INSTRUCTIONS in system else 'review' if SHARD_REVIEW_PREAMBLE in system
                else 'merge' if MERGE_REVIEW_PREAMBLE in system else 'revision' if SHARD_REVISION_PREAMBLE in system else '?')
        self.conversations.append((role, copy.deepcopy(messages)))
        return json.dumps(getattr(self, role)(messages, user))

    def author(self, messages, user):
        shard = next(s for s in PLAN.shards if user['shard']['id'].startswith(s.id))
        if len(messages) == 2:
            return {'tool': 'read_files', 'paths': list(shard.paths)[:8]}
        if self.invalid_author:
            self.invalid_author -= 1
            return {'shard_summary': {'criteria': 'not a list'}}
        first = shard.paths[0]
        return {'shard_summary': {'criteria': [criterion(first, first), criterion('README.md', 'root make test')],
                                  'cross_references': [], 'questions': [AUTHOR_ONLY]}}

    def review(self, messages, user):
        self.reviews += 1
        ids = [c['id'] for c in user['criterion_index']]
        decisions = [decision(fid, ids[:1]) for fid in user['required_decision_ids']]
        shard_files = [item['path'] for item in user['mandatory_evidence']]
        if self.revise_shard is not None and shard_files and shard_files[0].startswith(self.revise_shard) and not user['required_decision_ids']:
            finding = {'category': 'coverage', 'issue': 'Missing a service obligation', 'sources': [shard_files[0]],
                       'affected_ids': [], 'gap_kind': 'missing_obligation', 'inspection_requests': []}
            return {'scope_assessment': {'verdict': 'revise', 'rationale': 'Gap', 'sources': [shard_files[0]],
                                         'findings': [finding], 'decisions': decisions}}
        return {'scope_assessment': {'verdict': 'accept', 'rationale': 'Covered', 'sources': ['README.md'],
                                     'findings': [], 'decisions': decisions}}

    def merge(self, messages, user):
        return self.review(messages, {**user, 'mandatory_evidence': []})

    def revision(self, messages, user):
        evidence = [item['path'] for item in user['mandatory_evidence']]
        return {'scope_revision': {'base_digest': user['scope_base_digest'],
                                   'add': [criterion(evidence[0], 'added service obligation')], 'reassess': []},
                'finding_responses': [{'finding_id': fid, 'rationale': 'Added the missing obligation',
                                       'sources': [evidence[0]], 'affected_ids': []} for fid in user['required_response_ids']]}


async def run(router, *, budget=None, limits=LIMITS, entries=ENTRIES):
    from coordinare.services.environment_scope import discover_scope

    async def fetch(path):
        return SOURCES.get(path)
    return await discover_scope(paths=list(SOURCES), fetch=fetch, chat=router, commit=COMMIT, limits=limits,
                                budget=budget or AttemptBudget(max_calls=1024, max_reads=8192, max_coverage_revisions=8),
                                entries=entries)


def test_plan_shards_this_fixture():
    assert needs_sharding(ENTRIES, context_tokens=TOKENS, max_file_bytes=LIMITS.max_file_bytes)
    assert PLAN.spine == ('README.md',) and len(PLAN.shards) >= 2


@pytest.mark.asyncio
async def test_sharded_scope_accepts_one_definition_and_one_ledger():
    budget = AttemptBudget(max_calls=1024, max_reads=8192, max_coverage_revisions=8)
    contract = await run(Router(), budget=budget)
    assert budget.scope_phase == 'accepted' and len(contract.finding_ledger.reviews) == 1
    cited = {path for c in contract.definition.criteria for path in c.sources}
    assert {shard.paths[0] for shard in PLAN.shards} <= cited
    # The root obligation every shard reported merges into one criterion.
    assert sum(c.description == 'Obligation root make test' for c in contract.definition.criteria) == 1
    assert budget.shard_agents >= 2 * len(PLAN.shards) and budget.schema_repairs == 0


@pytest.mark.asyncio
async def test_agent_reads_only_its_shard():
    router = Router()
    await run(router)
    for role, messages in router.conversations:
        if role != 'author':
            continue
        user = json.loads(messages[1]['content'])
        shard = next(s for s in PLAN.shards if s.id == user['shard']['id'])
        listed = {entry['path'] for entry in user['root_directory']['entries']}
        allowed_tops = {path.split('/')[0] for path in (*shard.paths, *PLAN.spine)}
        assert listed <= allowed_tops


@pytest.mark.asyncio
async def test_reviewers_never_receive_author_material():
    router = Router()
    await run(router)
    reviews = [messages for role, messages in router.conversations if role in {'review', 'merge'}]
    assert reviews and all(AUTHOR_ONLY not in json.dumps(messages) for messages in reviews)
    assert all(len(messages) == 2 for messages in reviews)  # fresh conversations


@pytest.mark.asyncio
async def test_revise_round_reruns_only_open_shards_and_shares_coverage_revisions():
    router = Router(revise_shard='svc1')
    budget = AttemptBudget(max_calls=1024, max_reads=8192, max_coverage_revisions=8)
    contract = await run(router, budget=budget)
    assert len(contract.finding_ledger.reviews) == 2 and budget.coverage_revisions == 1
    assert any(c.description == 'Obligation added service obligation' for c in contract.definition.criteria)
    roles = [role for role, _messages in router.conversations]
    first_review = roles.index('review')
    revision = roles.index('revision')
    later_reviews = [role for role in roles[revision:] if role == 'review']
    assert roles.count('revision') == 1
    assert len(later_reviews) < roles[first_review:revision].count('review')


@pytest.mark.asyncio
async def test_each_shard_agent_has_its_own_two_corrections():
    budget = AttemptBudget(max_calls=1024, max_reads=8192, max_coverage_revisions=8)
    await run(Router(invalid_author=2), budget=budget)
    assert budget.shard_schema_repairs == 2 and budget.schema_repairs == 0


@pytest.mark.asyncio
async def test_a_shard_agent_beyond_two_corrections_holds():
    class Stubborn(Router):
        def author(self, messages, user):
            if len(messages) == 2:
                return super().author(messages, user)
            return {'shard_summary': {'criteria': 'not a list'}}

    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await run(Stubborn())
    assert failure.value.code == 'attempt_schema_limit'


@pytest.mark.asyncio
async def test_small_repository_keeps_the_single_conversation_path(monkeypatch):
    import coordinare.services.environment_shard_scope as sharded

    async def refuse(*args, **kwargs):
        raise AssertionError('sharded path used for a repository that fits one conversation')
    monkeypatch.setattr(sharded, 'discover_sharded_scope', refuse)
    small = {'README.md': ENTRIES['README.md']}
    assert not needs_sharding(small, context_tokens=TOKENS, max_file_bytes=LIMITS.max_file_bytes)


def summary(shard_id, criteria, refs=()):
    return ShardSummary(shard_id=shard_id, criteria=[ScopeCriterionProposal(**c) for c in criteria],
                        cross_references=list(refs), questions=[], inputs={})


def test_duplicate_criteria_merge_without_downgrade():
    weak = {**criterion('a/x', 'same'), 'applicability': 'unknown'}
    strong = criterion('b/y', 'same')
    [merged] = merge_summaries([summary('shard-000-00000000', [weak]), summary('shard-001-00000000', [strong])])
    assert merged.applicability == 'required' and merged.sources == ['a/x', 'b/y']


def test_cross_reference_routed_once():
    first, second = PLAN.shards[0], PLAN.shards[1]
    cycle = [summary(first.id, [], refs=[second.paths[-1]]), summary(second.id, [], refs=[first.paths[-1]])]
    assigned: set[str] = set()
    follow = route_cross_references(cycle, PLAN, read=set(), assigned=assigned, generation=1)
    assert sorted(s.paths for s in follow) == sorted([(second.paths[-1],), (first.paths[-1],)])
    assert route_cross_references(cycle, PLAN, read=set(), assigned=assigned, generation=2) == []


def assessment(verdict, decisions=(), findings=()):
    return ScopeAssessmentProposal.model_validate({'verdict': verdict, 'rationale': 'r', 'sources': ['README.md'],
        'findings': list(findings), 'decisions': list(decisions)})


def test_accept_requires_every_part_and_merge_disposition_wins():
    from coordinare_service_inference.environment.findings import FindingLedger
    ledger = FindingLedger(source_commit=COMMIT)
    parts = [('shard-000-00000000', assessment('accept'), {}), (MERGE_OWNER, assessment('accept'), {})]
    assert combine_round(parts, ledger=ledger)['scope_assessment']['verdict'] == 'accept'
    finding = {'category': 'coverage', 'issue': 'gap', 'sources': ['README.md'], 'affected_ids': [],
               'gap_kind': 'missing_obligation', 'inspection_requests': []}
    parts[0] = ('shard-000-00000000', assessment('revise', findings=[finding]), {})
    assert combine_round(parts, ledger=ledger)['scope_assessment']['verdict'] == 'revise'
    parts.append(('shard-001-00000000', assessment('indeterminate'), {}))
    assert combine_round(parts, ledger=ledger)['scope_assessment']['verdict'] == 'indeterminate'
