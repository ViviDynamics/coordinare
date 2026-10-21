"""Oversized files and symlinks are listed but never read; asking for one never ends an attempt."""
from __future__ import annotations

import pytest
from coordinare_service_inference.environment.scope_investigation import ScopeInspectionBinding

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_inspection import InventoryEntry, SourceInspection
from coordinare.services.environment_scope_investigation import investigate_scope_requests

BIG = 300 * 1024


def entries():
    return {'README.md': InventoryEntry('README.md', 100, '100644'),
            'data/huge.json': InventoryEntry('data/huge.json', BIG, '100644'),
            '.opencode/skill': InventoryEntry('.opencode/skill', 17, '120000')}


def inspection(fetched=None, *, with_entries=True, budget=None):
    async def fetch(path):
        if fetched is not None:
            fetched.append(path)
        return None if path == '.opencode/skill' else 'content'
    return SourceInspection(sorted(entries()), fetch, DiscoveryLimits(), budget,
                            entries=entries() if with_entries else None)


def binding(source, path):
    return ScopeInspectionBinding.bind(finding_id='F0001', review_sequence=1, scope_digest='1'*64,
        source_commit='a'*40, inventory_digest=source.inventory_digest,
        request={'operation': 'read_file', 'path': path})


@pytest.mark.parametrize(('path', 'status'), [('data/huge.json', 'too_large'), ('.opencode/skill', 'link'),
                                              ('README.md', 'readable')])
def test_policy_status(path, status):
    assert inspection().policy_status(path) == status


@pytest.mark.asyncio
@pytest.mark.parametrize(('path', 'code'), [('data/huge.json', 'too_large'), ('.opencode/skill', 'link')])
async def test_navigation_read_answers_without_fetch_or_charge(path, code):
    fetched = []
    budget = AttemptBudget()
    source = inspection(fetched, budget=budget)
    result = await source.tool_result({'tool': 'read_file', 'path': path})
    assert result['errors'] == [{'index': 0, 'code': code}]
    assert fetched == [] and budget.reads == 0 and path not in source.inputs


@pytest.mark.asyncio
@pytest.mark.parametrize(('path', 'status'), [('data/huge.json', 'too_large'), ('.opencode/skill', 'link')])
async def test_investigation_returns_policy_evidence_instead_of_holding(path, status):
    fetched = []
    source = inspection(fetched)
    [result] = await investigate_scope_requests(source, [binding(source, path)])
    assert result.status == status and result.file_sha256 is None and fetched == []


@pytest.mark.asyncio
async def test_without_entries_behaviour_is_unchanged():
    source = inspection(with_entries=False)
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await investigate_scope_requests(source, [binding(source, '.opencode/skill')])
    assert failure.value.code == 'scope_investigation_unavailable'


def test_unread_source_gap_cannot_rest_on_unreadable_file():
    from coordinare_service_inference.environment.scope import ScopeAssessmentProposal

    from coordinare.services.environment_scope import _request_rule_errors
    source = inspection(budget=AttemptBudget())
    def assessment(path):
        return ScopeAssessmentProposal.model_validate({'verdict': 'revise', 'rationale': 'Judgment', 'sources': ['README.md'],
            'findings': [{'category': 'coverage', 'issue': 'Unread', 'sources': ['README.md'], 'affected_ids': [],
                'gap_kind': 'unread_source', 'inspection_requests': [{'operation': 'read_file', 'path': path}]}],
            'decisions': []})
    assert [e['code'] for e in _request_rule_errors(assessment('.opencode/skill'), reviewer=source)] == ['scope_gap_request_invalid']
    assert [e['code'] for e in _request_rule_errors(assessment('data/huge.json'), reviewer=source)] == ['scope_gap_request_invalid']
    assert _request_rule_errors(assessment('README.md'), reviewer=source) == []


@pytest.mark.asyncio
async def test_snapshot_reports_sizes_and_modes(tmp_path):
    import subprocess

    from scripts.validate_environment_repositories import Snapshot
    repo = tmp_path/'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@e.invalid',
                               '-c', 'commit.gpgsign=false', *args], check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-q')
    (repo/'README.md').write_text('hello')
    (repo/'big.bin').write_bytes(b'x'*BIG)
    (repo/'link').symlink_to('README.md')
    git('add', '-A')
    git('commit', '-qm', 'fixture')
    commit = git('rev-parse', 'HEAD')
    entries = await Snapshot(repo, commit, 'org', 'repo').get_repository_entries('org', 'repo', ref=commit)
    by_path = {entry.path: entry for entry in entries}
    assert by_path['README.md'] == InventoryEntry('README.md', 5, '100644')
    assert by_path['big.bin'].size == BIG and by_path['link'].mode == '120000'


@pytest.mark.asyncio
async def test_github_tree_reports_sizes_and_modes(monkeypatch):
    import httpx

    from coordinare.services.github import GitHubService
    tree = {'truncated': False, 'tree': [
        {'path': 'README.md', 'type': 'blob', 'mode': '100644', 'size': 5},
        {'path': '.opencode/skill', 'type': 'blob', 'mode': '120000', 'size': 17},
        {'path': 'src', 'type': 'tree', 'mode': '040000'}]}
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=tree)), **kw))
    service = GitHubService.__new__(GitHubService)
    async def token():
        return 'fixture'
    monkeypatch.setattr(service, '_current_token', token, raising=False)
    monkeypatch.setattr(service, '_rest_api_base', lambda: 'https://api.github.invalid', raising=False)
    entries = await service.get_repository_entries('org', 'repo', ref='a'*40)
    assert entries == [InventoryEntry('.opencode/skill', 17, '120000'), InventoryEntry('README.md', 5, '100644')]


@pytest.mark.asyncio
async def test_scope_discovery_uses_entries_for_investigation():
    from tests.unit.test_353_scope_contract import Replies, criterion, proposal, revision, run
    from tests.unit.test_353_source_decision_flow import READ_GUIDE, accept_covered, assess, gap_finding
    link = {'operation': 'read_file', 'path': 'linked'}
    replies = Replies([READ_GUIDE, proposal(criterion()),
        assess('revise', findings=[gap_finding(gap_kind='missing_obligation', inspection_requests=[link])]),
        revision(), accept_covered('F0001')])
    budget = AttemptBudget()
    sources = {'guide': 'Develop locally. See odd/nest for operational service requirements.',
               'odd/nest': 'A local service must accept a request.'}
    async def fetch(path):
        return sources.get(path)
    entries = {**{path: InventoryEntry(path, len(text), '100644') for path, text in sources.items()},
               'linked': InventoryEntry('linked', 5, '120000')}
    await run(replies, budget=budget, fetch=fetch, paths=sorted(entries), entries=entries)
    assert [result.status for result in budget.scope_investigation_results] == ['link']


@pytest.mark.asyncio
async def test_env_cache_passes_provider_entries_into_discovery(tmp_path, monkeypatch):
    from coordinare.services import env_cache as service_module
    from tests.unit.scope_chat_fixture import ScopeChatFixture
    from tests.unit.test_353_bootstrap_pipeline import Proposal, Repository, setup
    service, cache, _, capability = setup(tmp_path)
    repository = Repository()
    async def get_repository_entries(owner, repo, *, ref, max_entries=100_000):
        return [InventoryEntry(path, 10, '100644') for path in repository.paths]
    repository.get_repository_entries = get_repository_entries
    seen = {}
    async def discover(**kwargs):
        seen.update(kwargs)
        raise EnvironmentDiscoveryError('stop after capture', code='capture')
    monkeypatch.setattr(service_module, 'discover_and_review', discover, raising=False)
    import coordinare.services.environment_discovery as discovery_module
    monkeypatch.setattr(discovery_module, 'discover_and_review', discover)
    try:
        await service._build_manifest_artifacts(performer_mounts=(cache.cache_dir.parent,), symphony_name='suite',
            repo='suite', github_org='org', github_service=repository, readme_contents={}, current_sha='fingerprint',
            cache_dir=cache.cache_dir, cache_mount_path=capability.cache_mount_path,
            llm_chat=ScopeChatFixture(Proposal(), 'guide'), cache_state=cache, capabilities=capability)
    except EnvironmentDiscoveryError:
        pass
    assert seen['paths'] == ['guide']
    assert seen['entries'] == {'guide': InventoryEntry('guide', 10, '100644')}
