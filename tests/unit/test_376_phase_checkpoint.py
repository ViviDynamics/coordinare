"""Native checkpoint restores private tool state without inventing delivered pages."""
from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

import pytest

from coordinare.services.agent_runtime.checkpoint import CheckpointStore
from coordinare.services.agent_runtime.nare_runtime import NareRuntime
from coordinare.services.environment_agent import ObservationTools, PinnedSourceTools
from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from coordinare.services.environment_inspection import SourceInspection
from tests.unit.test_376_agent_accounting import admit
from tests.unit.test_376_agent_runtime import ScriptedTransport, request


def navigation(text: str) -> PinnedSourceTools:
    async def fetch(path: str) -> str:
        return text
    return PinnedSourceTools(SourceInspection(['guide'], fetch, DiscoveryLimits()))


@pytest.mark.asyncio
async def test_partial_dynamic_source_and_observation_coverage_survives_resume() -> None:
    from coordinare.services.environment_agent import PhaseToolState

    original = navigation('evidence漢字' * 9000)
    observations = ObservationTools()
    first = json.loads(await original.read({'path': 'guide'}))
    report = json.loads(await observations.add({'result': 'observation漢字' * 8000}))
    assert first['unread_count'] > 0 and report['unread_count'] > 0
    saved = PhaseToolState(original, observations).snapshot()
    restored = navigation('must reuse immutable cached bytes')
    new_observations = ObservationTools()
    await PhaseToolState(restored, new_observations).restore(saved)
    assert restored.inspection.inputs == original.inspection.inputs
    assert restored.inspection.reads == original.inspection.reads == 1
    assert restored.inspection.total_bytes == original.inspection.total_bytes
    assert restored.catalogs['guide'].receipt() == original.catalogs['guide'].receipt()
    assert new_observations.catalogs[report['observation_id']].receipt() == observations.catalogs[report['observation_id']].receipt()
    assert restored.issues() and new_observations.issues()
    next_page = json.loads(await restored.read({'path': 'guide'}))
    assert next_page['pages'][0]['id'] == first['next_unread']


@pytest.mark.asyncio
async def test_dynamic_source_hash_or_current_binding_change_refuses_restore() -> None:
    from coordinare.services.environment_agent import PhaseToolState

    original = navigation('pinned source')
    await original.read({'path': 'guide'})
    saved = PhaseToolState(original, None).snapshot()
    restored = navigation('different')
    restored.inspection.inputs['guide'] = hashlib.sha256(b'different').hexdigest()
    with pytest.raises(EnvironmentDiscoveryError):
        await PhaseToolState(restored, None).restore(saved)
    saved['sources']['guide']['content'] = 'corrupted cache'
    with pytest.raises(EnvironmentDiscoveryError):
        await PhaseToolState(navigation('pinned source'), None).restore(saved)


@pytest.mark.asyncio
async def test_runtime_checkpoints_tool_state_and_shard_accounting(tmp_path: Any) -> None:
    from coordinare.services.environment_agent import PhaseToolState

    original = navigation('pinned source' * 6000)
    await original.read({'path': 'guide'})
    state = PhaseToolState(original, None)
    req = replace(request(admit), checkpoint_store=CheckpointStore(tmp_path), tool_state=state)
    req.budget.shard_schema_repairs = 2
    result = await NareRuntime(transport=ScriptedTransport('{"accepted":true}')).run(req)
    saved = await req.checkpoint_store.load_verified(result.checkpoint_key, req.identity)
    assert saved.tool_state == state.snapshot()
    assert saved.counters['shard_schema_repairs'] == 2
    req.budget.shard_schema_repairs = 1
    transport = ScriptedTransport('{"accepted":true}')
    resumed = await NareRuntime(transport=transport).run(replace(req, checkpoint_key=result.checkpoint_key))
    assert resumed.hold_code == 'agent_checkpoint_accounting'
    assert not transport.requests


@pytest.mark.asyncio
async def test_completed_effect_before_first_page_restores_as_unread() -> None:
    from coordinare.services.agent_runtime.effects import EffectJournal
    from coordinare.services.environment_agent import PhaseToolState

    journal = EffectJournal()
    async def persist() -> None:
        pass
    async def execute() -> dict[str, Any]:
        return {'result': 'a complete but not yet delivered observation'}
    journal.persist = persist
    await journal.run('trial', 'a'*64, execute)
    observations = ObservationTools()
    restored = PhaseToolState(None, observations, journal)
    await restored.restore({})
    assert len(observations.catalogs) == 1
    catalog = next(iter(observations.catalogs.values()))
    assert catalog.remaining_required and not catalog.delivered
    assert observations.issues()[0].code == 'runtime_observation_unread'


@pytest.mark.asyncio
async def test_prerequisite_reminder_cannot_reset_at_resume(tmp_path: Any) -> None:
    from coordinare.services.agent_runtime.models import AdmissionIssue

    async def missing(answer: Any) -> list[Any]:
        return [AdmissionIssue('preflight_required', 'Execute the approved probe.')]
    req = replace(request(missing), checkpoint_store=CheckpointStore(tmp_path), prerequisites=frozenset({'preflight_required'}))
    req.budget.max_schema_repairs = 0
    transport = ScriptedTransport('{"accepted":true}')
    runtime = NareRuntime(transport=transport)
    result = await runtime.run(req)
    assert result.hold_code == 'preflight_required'
    assert req.budget.schema_repairs == 0

    saved = await req.checkpoint_store.load_verified(result.checkpoint_key, req.identity)
    assert saved.prerequisite_reminders == frozenset({'preflight_required'})
    before = len(transport.requests)
    held = await runtime.run(replace(req, checkpoint_key=result.checkpoint_key))
    assert held.hold_code == 'preflight_required'
    assert len(transport.requests) == before
    # Construct a nonterminal checkpoint to isolate reminder persistence. Merely
    # changing native status does not reopen a domain-held checkpoint.
    from nare import dumps, loads

    session = loads(saved.session_text)
    session.status, session.error, session.stop_reason = 'working', None, None
    key = await req.checkpoint_store.save(saved.model_copy(update={
        'session_text': dumps(session), 'terminal_status': None, 'hold_code': None}))
    resumed = await runtime.run(replace(req, checkpoint_key=key))
    assert resumed.hold_code == 'preflight_required'
    assert len(transport.requests) == before + 1
    assert req.budget.schema_repairs == 0


@pytest.mark.asyncio
async def test_completed_checkpoint_is_revalidated_without_another_provider_call(tmp_path: Any) -> None:
    checked = []
    async def validate(answer: Any) -> list[Any]:
        checked.append(answer)
        return []
    req = replace(request(validate), checkpoint_store=CheckpointStore(tmp_path))
    transport = ScriptedTransport('{"accepted":true}')
    runtime = NareRuntime(transport=transport)
    first = await runtime.run(req)
    second = await runtime.run(replace(req, checkpoint_key=first.checkpoint_key))
    assert second.status == 'accepted' and second.answer == first.answer
    assert checked == [{'accepted': True}, {'accepted': True}]
    assert len(transport.requests) == req.budget.calls == 1


def test_restart_restores_trial_and_sharded_correction_spend(tmp_path: Any) -> None:
    from coordinare.services.environment_repair import begin_attempt, save_attempt
    from tests.unit.test_353_execution_repair import cache_fixture

    cache = cache_fixture(tmp_path)
    bounds = DiscoveryLimits()
    original = begin_attempt(cache, bounds, outer_seconds=600)
    original.trial_runs, original.shard_schema_repairs = 2, 7
    save_attempt(cache, original)
    restarted = begin_attempt(cache, bounds, outer_seconds=600)
    assert restarted.trial_runs == 2 and restarted.shard_schema_repairs == 7
    # A caller with stale counters cannot erase previously spent authority.
    restarted.trial_runs, restarted.shard_schema_repairs = 0, 0
    save_attempt(cache, restarted)
    again = begin_attempt(cache, bounds, outer_seconds=600)
    assert again.trial_runs == 2 and again.shard_schema_repairs == 7


@pytest.mark.asyncio
async def test_phase_deadline_is_not_renewed_by_checkpoint_resume(tmp_path: Any, monkeypatch: Any) -> None:
    import time

    req = replace(request(admit), checkpoint_store=CheckpointStore(tmp_path), phase_deadline=time.monotonic() + 60)
    runtime = NareRuntime(transport=ScriptedTransport('{"accepted":true}'))
    first = await runtime.run(req)
    assert first.status == 'accepted'
    later = time.time() + 61
    monkeypatch.setattr(time, 'time', lambda: later)
    resumed = await runtime.run(replace(req, checkpoint_key=first.checkpoint_key, phase_deadline=time.monotonic() + 60))
    assert resumed.status == 'held' and resumed.hold_code == 'phase_deadline'
    assert req.budget.calls == 1


@pytest.mark.asyncio
async def test_phase_automatically_reuses_only_its_verified_journal_reference(tmp_path: Any) -> None:
    from coordinare.services.environment_agent import run_environment_phase
    from tests.unit.test_353_review_findings_flow import review
    from tests.unit.test_376_environment_review import ReviewerTransport, phase

    transport = ReviewerTransport(review(), early=False)
    req = phase(tmp_path, transport)
    first = await run_environment_phase(req)
    calls = transport.calls
    second = await run_environment_phase(req)
    assert second.data == first.data
    assert transport.calls == calls
    assert second.evidence.receipt() == first.evidence.receipt()


@pytest.mark.asyncio
async def test_verified_provider_failure_checkpoint_resumes_without_resetting_accounting(tmp_path: Any) -> None:
    class Unavailable(ScriptedTransport):
        async def turn(self, messages: Any, tools: Any) -> Any:
            raise RuntimeError('Synthetic provider unavailable')
    req = replace(request(admit), checkpoint_store=CheckpointStore(tmp_path))
    failed = await NareRuntime(transport=Unavailable('')).run(req)
    assert failed.hold_code == 'provider_request_failed'
    assert req.budget.calls == req.budget.agent_usage_unknown == 1
    resumed = await NareRuntime(transport=ScriptedTransport('{"accepted":true}')).run(
        replace(req, checkpoint_key=failed.checkpoint_key))
    assert resumed.status == 'accepted'
    assert req.budget.calls == 2 and req.budget.agent_usage_unknown == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(('mode', 'size', 'code'), [('120000', 4, 'link'), ('100644', 999999999, 'too_large')])
async def test_native_navigation_respects_source_inventory_policy(mode: str, size: int, code: str) -> None:
    from unittest.mock import AsyncMock

    from coordinare.services.environment_inspection import InventoryEntry

    fetch = AsyncMock(return_value='must not be fetched')
    inspection = SourceInspection({'guide': InventoryEntry(path='guide', mode=mode, size=size)},
                                  fetch, DiscoveryLimits())
    sources = PinnedSourceTools(inspection)
    result = json.loads(await sources.read({'path': 'guide'}))
    assert result['error'] == code
    assert not sources.catalogs and not inspection.inputs
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_restored_source_observer_preserves_domain_exception() -> None:
    from coordinare.services.environment_agent import PhaseToolState
    from coordinare.services.environment_construction import ScopeReopenedError

    original = navigation('new obligation')
    await original.read({'path': 'guide'})
    restored = navigation('new obligation')
    failure = ScopeReopenedError()
    async def observe(inputs: Any) -> None:
        raise failure
    restored.observe_sources = observe
    with pytest.raises(ScopeReopenedError):
        await PhaseToolState(restored, None).restore(PhaseToolState(original, None).snapshot())
    assert restored.failure is failure
