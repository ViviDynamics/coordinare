"""Inventory sizes, modes and link targets come from the verified pinned execution-source bundle."""
from __future__ import annotations

import json

import pytest
from coordinare_service_inference.environment.execution_source import (
    ExecutionSourceError,
    bundle_inventory,
)
from coordinare_service_inference.environment.inventory import InventoryEntry
from coordinare_service_inference.environment.scope import (
    MAX_SCOPE_CRITERIA,
    ScopeCriterion,
    ScopeCriterionProposal,
    ScopeDefinition,
    criterion_id,
)

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from tests.unit.test_353_execution_source import FILES, bundle_file


def test_bundle_inventory_reports_sizes_modes_and_link_targets(tmp_path):
    path, identity = bundle_file(tmp_path, FILES)
    entries = bundle_inventory(path, expected=identity)
    link = next(entry for entry in entries.values() if entry.mode == '120000')
    assert link.target == FILES[link.path][0].decode()
    regular = next(entry for entry in entries.values() if entry.mode != '120000')
    assert regular.size == len(FILES[regular.path][0]) and regular.target is None
    assert set(entries) == set(FILES)


def test_bundle_inventory_rejects_a_bundle_for_another_identity(tmp_path):
    path, identity = bundle_file(tmp_path, FILES)
    other = {**identity, 'tree_digest': '0'*64}
    with pytest.raises(ExecutionSourceError):
        bundle_inventory(path, expected=type(identity)(other) if not isinstance(identity, dict) else other)


@pytest.mark.asyncio
async def test_env_cache_takes_entries_from_the_bundle_and_refuses_mismatched_paths(tmp_path):
    from coordinare.services.env_cache import _pinned_inventory

    class Provider:
        def __init__(self, paths):
            self.paths = paths
        async def get_repository_paths(self, owner, repo, *, ref, max_entries=100_000):
            return list(self.paths)
    from types import SimpleNamespace

    from coordinare_service_inference.environment.execution_source import ExecutionSourceIdentity
    path, identity = bundle_file(tmp_path, FILES)
    bundle = SimpleNamespace(payload_path=path, identity=ExecutionSourceIdentity.model_validate(identity))
    paths, entries = await _pinned_inventory(Provider(sorted(FILES)), 'org', 'repo', limit=1000, bundle=bundle)
    assert paths == sorted(FILES) and set(entries) == set(FILES)
    with pytest.raises(ExecutionSourceError) as failure:
        await _pinned_inventory(Provider([*sorted(FILES), 'extra']), 'org', 'repo', limit=1000, bundle=bundle)
    assert failure.value.code == 'source_identity'


TEXT = 'Développement documenté: opération réussie, sortie observable, 開発環境の確認. ' * 5


def _definition(count):
    proposals = [ScopeCriterionProposal(description=f'Obligation {i}: {TEXT}', sources=['guide'], applicability='required',
                 rationale=TEXT[:400], usability=TEXT[:400], runtime_needs=[]) for i in range(count)]
    return ScopeDefinition(source_commit='a'*40, inventory_digest='b'*64, inputs={'guide': 'c'*64},
                           criteria=[ScopeCriterion(**p.model_dump(), id=criterion_id(p)) for p in proposals])


def test_large_non_ascii_scope_fits_manifest_and_journal_bounds():
    from coordinare_service_inference.environment.models import MAX_DOCUMENT_BYTES

    from coordinare.services.environment_repair import JOURNAL_MAX_BYTES
    definition = _definition(MAX_SCOPE_CRITERIA)
    data = definition.model_dump(mode='json')
    raw = len(json.dumps(data, ensure_ascii=False).encode())
    escaped = len(json.dumps(data).encode())
    assert raw <= 1024*1024
    # A journal holds the definition, the contract (which repeats it) and the ledger, ASCII-escaped.
    assert 3*escaped <= JOURNAL_MAX_BYTES
    # A manifest embeds the contract (definition plus review evidence) and criterion mappings.
    assert 2*raw + MAX_SCOPE_CRITERIA*200 <= MAX_DOCUMENT_BYTES


@pytest.mark.asyncio
async def test_scope_beyond_the_criterion_cap_holds_with_scope_size_limit():
    from tests.unit.test_353_scope_contract import Replies, criterion, run
    too_many = {'scope': {'criteria': [criterion(usability=f'Operation {i} works') for i in range(MAX_SCOPE_CRITERIA+1)]},
                'finding_responses': []}
    replies = Replies([{'tool': 'read_file', 'path': 'guide'}, too_many])
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        await run(replies, budget=AttemptBudget())
    assert failure.value.code == 'scope_size_limit'


def test_inventory_entry_defaults_to_no_target():
    assert InventoryEntry('a', 1, '100644').target is None
