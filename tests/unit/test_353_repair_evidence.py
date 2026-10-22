"""Repair receives complete pinned evidence before proposing any correction."""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_discovery import DiscoveryLimits
from tests.unit.scope_chat_fixture import discover_with_fixture_scope as discover_and_review
from tests.unit.test_353_discovery_repair import (
    CAPABILITIES,
    COMMIT,
    PATH,
    SOURCE,
    assessment,
    candidate,
)


async def repair_case(*, failure=None, budget=None, limits=None):
    from coordinare_service_inference.environment.models import EnvironmentManifest

    files = {PATH: SOURCE, **{f'evidence/{i}': f'required evidence {i}' for i in range(17)}}
    inputs = {path: hashlib.sha256(content.encode()).hexdigest() for path, content in files.items()}
    from tests.unit.test_351_environment_discovery import candidate as full_candidate
    previous = full_candidate()
    previous['inputs'] = inputs
    previous['inventory_digest'] = hashlib.sha256(json.dumps(sorted(files), separators=(',', ':')).encode()).hexdigest()
    fetched = []
    calls = []

    async def fetch(path):
        fetched.append(path)
        if path == PATH and failure in {'missing', 'drift'}:
            return None if failure == 'missing' else SOURCE + ' changed'
        return files[path]

    async def chat(messages):
        calls.append(messages)
        context = json.loads(messages[1]['content'])
        # Every previous input is verified and indexed; contents are read on demand, never preloaded.
        assert {item['path']: item['sha256'] for item in context['mandatory_evidence']} == {
            path: hashlib.sha256(text.encode()).hexdigest() for path, text in files.items()}
        assert all('content' not in item for item in context['mandatory_evidence'])
        if 'manifest' in context:
            return json.dumps(assessment(candidate()))
        return json.dumps({'manifest': candidate()})

    result = await discover_and_review(symphony_name='suite', commit=COMMIT, paths=sorted(files),
        capabilities=CAPABILITIES, fetch=fetch, chat=chat, budget=budget, limits=limits,
        previous_manifest=EnvironmentManifest.model_validate(previous), execution_feedback={'failed': True})
    return result, fetched, calls


@pytest.mark.asyncio
async def test_repair_receives_all_18_inputs_before_first_call():
    (manifest, review), fetched, calls = await repair_case()
    assert len(calls) == 2
    assert len(manifest.inputs) == len(review.inputs) == 18
    # Repair binding reuses inputs the same author inspection already verified for construction.
    assert len(fetched) == 72
    assert all(fetched.count(path) == 4 for path in manifest.inputs)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure,code', [('missing', 'scope_author_evidence_unavailable'), ('drift', 'source_identity'),
    ('context', 'scope_author_context_limit'), ('reads', 'attempt_read_limit'), ('checkpoint', 'attempt_checkpoint_failed')])
async def test_incomplete_repair_evidence_holds_before_model(failure, code):
    budget = AttemptBudget(max_reads=1 if failure == 'reads' else 512)
    async def checkpoint():
        if failure == 'checkpoint':
            raise EnvironmentDiscoveryError('checkpoint rejected', code='attempt_checkpoint_failed')
    budget.checkpoint = checkpoint
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        await repair_case(failure=failure, budget=budget,
            limits=DiscoveryLimits(max_review_context_bytes=100 if failure == 'context' else 1024*1024))
    assert exc.value.code == code


@pytest.mark.asyncio
async def test_cancelled_mandatory_repair_fetches_are_drained():
    from coordinare.services.environment_discovery import _Inspection
    from coordinare.services.environment_evidence import prepare_mandatory_evidence

    started = asyncio.Event()
    stopped = asyncio.Event()
    async def fetch(path):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    inspection = _Inspection('discovery', [PATH], fetch, DiscoveryLimits(), AttemptBudget())
    task = asyncio.create_task(prepare_mandatory_evidence(inspection, {PATH: 'a'*64}, phase='repair'))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
