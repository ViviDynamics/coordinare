"""New retained source evidence must reach independent reviewers and converge."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment.findings import FindingLedger
from coordinare_service_inference.environment.inventory import InventoryEntry

from coordinare.services import environment_shard_scope as scope_module
from coordinare.services.environment_attempt import AttemptBudget
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_inspection import SourceInspection
from coordinare.services.environment_shard_scope import ShardedScope
from coordinare.services.environment_shards import Shard

PATH = 'support/tool.txt'
TEXT = 'A retained local tool requirement.'
DIGEST = hashlib.sha256(TEXT.encode()).hexdigest()


def test_new_retained_input_schedules_its_owner_without_changing_criteria():
    shard = Shard(id='owner', paths=(PATH,), readable_bytes=len(TEXT), depth=0)
    budget = AttemptBudget()
    budget.scope_inputs = {PATH: DIGEST}
    budget.scope_shards = {'reviewed_criteria': {'owner': []}}
    scope = SimpleNamespace(budget=budget, work=SimpleNamespace(owners={PATH: 'owner'}, shards=[shard]))
    definition = SimpleNamespace(inputs={}, criteria=[])

    assert ShardedScope._review_targets(scope, definition, {}, {}) == [shard]
    assert ShardedScope._review_targets(scope, definition, {}, {PATH: DIGEST}) == []


@pytest.mark.asyncio
async def test_new_retained_input_is_delivered_to_its_planned_owner():
    budget = AttemptBudget()
    budget.scope_inputs = {PATH: DIGEST}
    async def fetch(path):
        assert path == PATH
        return TEXT
    inspection = SourceInspection({PATH: InventoryEntry(PATH, len(TEXT), '100644')}, fetch,
                                  DiscoveryLimits(), budget)
    scope = SimpleNamespace(budget=budget, plan=SimpleNamespace(spine=(), owners={PATH: 'owner'}))

    evidence = await ShardedScope._review_evidence(scope, inspection, SimpleNamespace(inputs={}),
                                                  paths=[PATH], owned=[])

    assert inspection.inputs == {PATH: DIGEST}
    assert any(item['path'] == PATH for item in evidence)


@pytest.mark.asyncio
async def test_retained_input_outside_authored_shards_gets_a_reference_reviewer(monkeypatch):
    budget = AttemptBudget()
    budget.scope_inputs = {PATH: DIGEST}
    budget.scope_findings = FindingLedger(source_commit='a' * 40)
    async def load_spine():
        pass
    scope = SimpleNamespace(budget=budget, load_spine=load_spine, _owned=lambda *_: {},
        _review_targets=lambda *_: [], work=SimpleNamespace(owners={}),
        plan=SimpleNamespace(spine=(), share=4096), entries={PATH: InventoryEntry(PATH, len(TEXT), '100644')},
        request=SimpleNamespace(limits=DiscoveryLimits()), concurrency=1)
    class ScheduledError(Exception):
        pass
    async def capture(work, *_args, **_kwargs):
        assert [path for shard in work if shard is not None for path in shard.paths] == [PATH]
        raise ScheduledError
    monkeypatch.setattr(scope_module, 'run_pool', capture)

    with pytest.raises(ScheduledError):
        await ShardedScope.review_round(scope, SimpleNamespace(inputs={}))
