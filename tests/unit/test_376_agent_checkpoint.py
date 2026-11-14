"""A saved conversation cannot renew authority, allowances or uncertain effects."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from nare import Usage, dumps, loads, new_session

from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from tests.unit.test_376_agent_runtime import request


def checkpoint() :
    from coordinare.services.agent_runtime.checkpoint import AgentCheckpoint

    session = new_session('Retain this original task.', session_id='saved-session')
    session.usage = Usage(input=7000, output=120, cache_read=400)
    session.turns = 7
    session.schema_corrections = 2
    return AgentCheckpoint(identity=request(None).identity, session_text=dumps(session),
                           evidence={'catalog_digest': '1' * 64, 'delivered': [0, 2]},
                           counters={'calls': 9, 'reads': 4, 'schema_repairs': 2})


@pytest.mark.asyncio
async def test_resume_preserves_usage_turns_and_corrections(tmp_path: Path) -> None:
    from coordinare.services.agent_runtime.checkpoint import CheckpointStore

    store = CheckpointStore(tmp_path)
    original = checkpoint()
    key = await store.save(original)
    restored = await store.load_verified(key, original.identity)
    session = loads(restored.session_text)
    assert session.id == 'saved-session'
    assert session.messages[0].content[0]['text'] == 'Retain this original task.'
    assert session.usage.total_tokens == 7520
    assert session.turns == 7
    assert session.schema_corrections == 2
    assert restored.counters == {'calls': 9, 'reads': 4, 'schema_repairs': 2}
    assert restored.evidence.delivered == (0, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('source_commit', 'b' * 40), ('role', 'author'),
    ('phase', 'scope'), ('attempt_id', 'other'), ('domain_digests', (('scope', '1' * 64),)),
    ('policy_digest', '1' * 64), ('prompt_digest', '1' * 64), ('schema_digest', '1' * 64),
    ('model_config_digest', '1' * 64)])
async def test_changed_identity_refuses_resume(tmp_path: Path, field: str, value: object) -> None:
    from coordinare.services.agent_runtime.checkpoint import CheckpointStore

    store = CheckpointStore(tmp_path)
    original = checkpoint()
    key = await store.save(original)
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await store.load_verified(key, replace(original.identity, **{field: value}))
    assert caught.value.code == 'agent_checkpoint_identity'


@pytest.mark.asyncio
async def test_tampering_cannot_be_hidden_by_rehashing_internal_fields(tmp_path: Path) -> None:
    from coordinare.services.agent_runtime.checkpoint import CheckpointStore

    store = CheckpointStore(tmp_path)
    original = checkpoint()
    key = await store.save(original)
    path = tmp_path / key
    raw = json.loads(path.read_text())
    raw['counters']['calls'] = 0
    path.write_text(json.dumps(raw))
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await store.load_verified(key, original.identity)
    assert caught.value.code == 'agent_checkpoint_integrity'


@pytest.mark.asyncio
async def test_partial_atomic_write_preserves_previous_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from coordinare.services.agent_runtime.checkpoint import CheckpointStore

    store = CheckpointStore(tmp_path)
    original = checkpoint()
    key = await store.save(original)

    def failed_replace(*args: object) -> None:
        raise OSError('simulated interruption')

    monkeypatch.setattr(os, 'replace', failed_replace)
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await store.save(original.model_copy(update={'counters': {'calls': 10}}))
    assert caught.value.code == 'agent_checkpoint_persistence'
    assert (await store.load_verified(key, original.identity)).counters['calls'] == 9
    assert sorted(p.name for p in tmp_path.iterdir()) == [key]


@pytest.mark.asyncio
@pytest.mark.parametrize('tool', ['approved_probe', 'repair_trial'])
async def test_pending_effect_holds_before_any_resume(tmp_path: Path, tool: str) -> None:
    from coordinare.services.agent_runtime.checkpoint import CheckpointStore, EffectReceipt

    store = CheckpointStore(tmp_path)
    original = checkpoint().model_copy(update={'effects': (EffectReceipt(
        operation_id='operation-a', tool=tool, binding_digest='2' * 64, status='pending'),)})
    key = await store.save(original)
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await store.load_verified(key, original.identity)
    assert caught.value.code == 'agent_effect_pending'


@pytest.mark.asyncio
async def test_checkpoint_reference_survives_domain_snapshot_without_budget_reset(tmp_path: Path) -> None:
    from coordinare.services.agent_runtime.checkpoint import CheckpointReference, CheckpointStore
    from coordinare.services.environment_discovery import DiscoveryLimits
    from coordinare.services.environment_repair import begin_attempt, journal_for, save_attempt
    from coordinare.state_store import EnvCacheStateSnapshot
    from tests.unit.test_353_execution_repair import cache_fixture

    cache = cache_fixture(tmp_path)
    budget = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=100)
    original = checkpoint()
    key = await CheckpointStore(tmp_path / 'checkpoints').save(original)
    budget.agent_checkpoints['review'] = CheckpointReference(key=key, identity=original.identity)
    budget.calls, budget.schema_repairs = 9, 2
    save_attempt(cache, budget)
    snapshot = EnvCacheStateSnapshot(symphony_name='synthetic', sanitised_name='synthetic', cache_dir='cache',
                                     environment_attempt=cache.environment_attempt)
    cache.environment_attempt = EnvCacheStateSnapshot.model_validate_json(snapshot.model_dump_json()).environment_attempt
    resumed = begin_attempt(cache, DiscoveryLimits(), outer_seconds=3600, now=200)
    assert resumed.agent_checkpoints['review'].key == key
    assert resumed.calls == 9
    assert resumed.schema_repairs == 2
    assert journal_for(cache).expires_at == 700


@pytest.mark.asyncio
async def test_resume_revalidates_evidence_catalog_before_restoring_coverage(tmp_path: Path) -> None:
    from coordinare.services.agent_runtime.checkpoint import CheckpointStore
    from coordinare.services.agent_runtime.evidence import EvidenceRead, EvidenceStore
    from tests.unit.test_376_agent_evidence import source

    first = EvidenceStore((source('original'),), required=frozenset({'nested/custom.input'}))
    await first.read(EvidenceRead())
    saved = checkpoint().model_copy(update={'evidence': first.receipt()})
    disk = CheckpointStore(tmp_path)
    key = await disk.save(saved)
    verified = await disk.load_verified(key, saved.identity)
    changed = EvidenceStore((source('changed'),), required=frozenset({'nested/custom.input'}))
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        changed.restore_receipt(verified.evidence)
    assert caught.value.code == 'agent_evidence_changed'
    assert changed.remaining_required == 1
    same = EvidenceStore((source('original'),), required=frozenset({'nested/custom.input'}))
    same.restore_receipt(verified.evidence)
    assert same.remaining_required == 0


@pytest.mark.asyncio
async def test_real_nare_continues_after_interrupted_inference_without_replaying_tool(tmp_path: Path) -> None:
    import asyncio

    from nare import Policy, Reply, ToolCall, run

    from coordinare.services.agent_runtime.checkpoint import CheckpointStore
    from coordinare.services.agent_runtime.evidence import EvidenceStore
    from tests.unit.test_376_agent_evidence import source
    from tests.unit.test_376_agent_runtime import ScriptedTransport

    evidence = EvidenceStore((source('immutable fact'),), required=frozenset({'nested/custom.input'}))
    second_call = asyncio.Event()

    class Interrupted(ScriptedTransport):
        async def turn(self, messages, tools):
            if self.requests:
                second_call.set()
                await asyncio.Future()
            self.requests.append({'messages': messages})
            call = ToolCall('read-1', 'review_evidence', {})
            return Reply([{'type': 'tool_use', 'id': call.id, 'name': call.name, 'input': {}}],
                         [call], Usage(input=100, output=10), 'tool_use')

    session = new_session('Read the evidence then give a verified answer.')
    policy = Policy(tools=frozenset({'review_evidence'}), pending=frozenset({'review_evidence'}))

    async def first_run():
        async for _ in run(session, transport=Interrupted(''), policy=policy, tools=(evidence.tool(),)):
            pass

    task = asyncio.create_task(first_run())
    await asyncio.wait_for(second_call.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.turns == 1
    saved = checkpoint().model_copy(update={'session_text': dumps(session), 'evidence': evidence.receipt()})
    disk = CheckpointStore(tmp_path)
    key = await disk.save(saved)
    verified = await disk.load_verified(key, saved.identity)
    resumed = loads(verified.session_text)
    evidence.restore_receipt(verified.evidence)
    transport = ScriptedTransport('{"accepted":true}')
    async for _ in run(resumed, transport=transport, policy=policy, tools=(evidence.tool(),)):
        pass
    assert resumed.status == 'done'
    assert resumed.turns == 2
    assert resumed.usage.total_tokens == 128
    assert resumed.tool_calls == 1
    assert len(transport.requests) == 1
