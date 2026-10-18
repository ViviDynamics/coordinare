"""Mandatory pinned evidence reaches each fresh reviewer before judgment."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from coordinare.services.environment_attempt import AttemptBudget
from coordinare.services.environment_discovery import (
    DiscoveryLimits,
    EnvironmentDiscoveryError,
    discover_and_review,
)
from tests.unit.test_353_discovery_repair import (
    CAPABILITIES,
    COMMIT,
    PATH,
    SOURCE,
    assessment,
    candidate,
    finding_responses,
)


@pytest.mark.asyncio
async def test_reviewer_receives_all_mandatory_contents_before_first_call():
    files = {PATH: SOURCE, **{f'evidence-{i}': f'constraint {i}' for i in range(7)}}
    fetched = []
    review_calls = []
    async def fetch(path):
        fetched.append(path)
        return files[path]
    async def chat(messages):
        context = json.loads(messages[1]['content'])
        if 'manifest' in context:
            review_calls.append(copy.deepcopy(messages))
            evidence = context['mandatory_evidence']
            assert {item['path']: item['content'] for item in evidence} == files
            assert all(item['sha256'] == hashlib.sha256(files[item['path']].encode()).hexdigest() for item in evidence)
            return json.dumps(assessment(candidate()))
        return json.dumps({'tool':'read_files', 'paths':list(files)}) if len(messages) == 2 else json.dumps({'manifest':candidate()})
    manifest, review = await discover_and_review(symphony_name='suite', commit=COMMIT, paths=list(files),
        capabilities=CAPABILITIES, fetch=fetch, chat=chat)
    assert len(review_calls) == 1
    assert all(fetched.count(path) == 2 for path in files)
    assert review.inputs == manifest.inputs


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['missing', 'drift', 'context', 'checkpoint'])
async def test_review_preparation_failure_never_requests_judgment(failure):
    reads = 0
    judgments = 0
    budget = AttemptBudget()
    async def checkpoint():
        if failure == 'checkpoint' and budget.stage == 'review_preparation':
            raise EnvironmentDiscoveryError('checkpoint failed', code='attempt_checkpoint_failed')
    budget.checkpoint = checkpoint
    async def fetch(path):
        nonlocal reads
        reads += 1
        if reads == 2:
            if failure == 'missing':
                return None
            if failure == 'drift':
                return SOURCE + ' changed'
        return SOURCE
    async def chat(messages):
        nonlocal judgments
        if 'manifest' in json.loads(messages[1]['content']):
            judgments += 1
            return json.dumps(assessment(candidate()))
        return json.dumps({'tool':'read_file', 'path':PATH}) if len(messages) == 2 else json.dumps({'manifest':candidate()})
    limits = DiscoveryLimits(max_review_context_bytes=100 if failure == 'context' else 1024*1024)
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        await discover_and_review(symphony_name='suite', commit=COMMIT, paths=[PATH],
            capabilities=CAPABILITIES, fetch=fetch, chat=chat, limits=limits, budget=budget)
    assert judgments == 0
    assert exc.value.code == {'missing':'review_evidence_unavailable', 'drift':'review_evidence_drift',
        'context':'review_context_limit', 'checkpoint':'attempt_checkpoint_failed'}[failure]
    assert reads == (1 if failure == 'checkpoint' else 2)


@pytest.mark.asyncio
async def test_revised_candidate_prefetches_expanded_input_set():
    files = {PATH: SOURCE, 'additional': 'additional required constraint'}
    review_starts = []
    fetched = []
    async def fetch(path):
        fetched.append(path)
        return files[path]
    async def chat(messages):
        context = json.loads(messages[1]['content'])
        if 'manifest' in context:
            if len(messages) == 2:
                review_starts.append({item['path'] for item in context['mandatory_evidence']})
                if len(review_starts) == 1:
                    return json.dumps({'tool':'read_file', 'path':'additional'})
            verdict = 'revise' if len(review_starts) == 1 else 'accept'
            result = assessment(candidate(), verdict, retained=len(review_starts) > 1)
            result['assessment']['sources'] = [PATH, 'additional']
            return json.dumps(result)
        if len(messages) == 2:
            return json.dumps({'tool':'read_file', 'path':PATH})
        if 'coverage_revision' in json.loads(messages[-1]['content']):
            return json.dumps({'tool':'read_file', 'path':'additional'})
        return json.dumps({'manifest':candidate(), 'finding_responses': finding_responses() if review_starts else []})
    _, review = await discover_and_review(symphony_name='suite', commit=COMMIT, paths=list(files),
        capabilities=CAPABILITIES, fetch=fetch, chat=chat)
    assert review_starts == [{PATH}, {PATH, 'additional'}]
    assert set(review.inputs) == set(files)
    assert fetched.count(PATH) == 3
    assert fetched.count('additional') == 3


@pytest.mark.asyncio
async def test_additional_review_reads_cannot_bypass_context_cap():
    review_calls = 0
    budget = AttemptBudget()
    async def fetch(path):
        return SOURCE if path == PATH else 'large evidence ' * 20000
    async def chat(messages):
        nonlocal review_calls
        context = json.loads(messages[1]['content'])
        if 'manifest' in context:
            review_calls += 1
            return json.dumps({'tool':'read_file', 'path':'additional'})
        return json.dumps({'tool':'read_file', 'path':PATH}) if len(messages) == 2 else json.dumps({'manifest':candidate()})
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        await discover_and_review(symphony_name='suite', commit=COMMIT, paths=[PATH, 'additional'],
            capabilities=CAPABILITIES, fetch=fetch, chat=chat, budget=budget,
            limits=DiscoveryLimits(max_review_context_bytes=100000, max_file_bytes=400000))
    assert exc.value.code == 'review_context_limit'
    assert review_calls == 1
    assert budget.mandatory_inputs_delivered == 1


@pytest.mark.asyncio
async def test_cancelled_review_preparation_drains_source_reads():
    import asyncio
    started, cancelled = asyncio.Event(), asyncio.Event()
    reads = 0
    async def fetch(path):
        nonlocal reads
        reads += 1
        if reads > 1:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return SOURCE
    async def chat(messages):
        assert 'manifest' not in json.loads(messages[1]['content'])
        return json.dumps({'tool':'read_file', 'path':PATH}) if len(messages) == 2 else json.dumps({'manifest':candidate()})
    task = asyncio.create_task(discover_and_review(symphony_name='suite', commit=COMMIT, paths=[PATH],
        capabilities=CAPABILITIES, fetch=fetch, chat=chat))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_elm_sequence_schema_then_two_coverage_revisions():
    discovery_outputs = [None, 'schema_invalid', 'valid', 'valid', 'valid']
    review_count = 0
    budget = AttemptBudget()
    async def fetch(path):
        return SOURCE
    async def chat(messages):
        nonlocal review_count
        context = json.loads(messages[1]['content'])
        if 'manifest' in context:
            assert {item['path'] for item in context['mandatory_evidence']} == {PATH}
            review_count += 1
            result = assessment(candidate(), 'revise' if review_count <= 2 else 'accept', retained=review_count > 1)
            result['assessment']['rationale'] = ('Missing required lanes' if review_count == 1 else
                'Conflicting pinned versions and no usability check' if review_count == 2 else 'Complete executable coverage')
            return json.dumps(result)
        step = discovery_outputs.pop(0)
        if step is None:
            return json.dumps({'tool':'read_file', 'path':PATH})
        value = candidate()
        if step == 'schema_invalid':
            value['activation']['env'] = {'UNSAFE': '${UNSAFE}:suffix'}
        return json.dumps({'manifest':value, 'finding_responses': finding_responses() if review_count else []})
    _, review = await discover_and_review(symphony_name='suite', commit=COMMIT, paths=[PATH],
        capabilities=CAPABILITIES, fetch=fetch, chat=chat, budget=budget)
    assert review.verdict == 'accept'
    assert review_count == 3
    assert (budget.schema_repairs, budget.coverage_revisions, budget.execution_repairs) == (1, 2, 0)


@pytest.mark.asyncio
async def test_preparation_phase_deadline_is_not_reported_as_aggregate_expiry():
    import asyncio
    reads = 0
    async def fetch(path):
        nonlocal reads
        reads += 1
        if reads > 1:
            await asyncio.Event().wait()
        return SOURCE
    async def chat(messages):
        return json.dumps({'tool':'read_file', 'path':PATH}) if len(messages) == 2 else json.dumps({'manifest':candidate()})
    events = []
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        await discover_and_review(symphony_name='suite', commit=COMMIT, paths=[PATH],
            capabilities=CAPABILITIES, fetch=fetch, chat=chat, diagnostics=events.append,
            limits=DiscoveryLimits(phase_timeout_seconds=0.02))
    assert exc.value.code == 'review_preparation_deadline'
    assert events[-1]['stage'] == 'review_preparation'
    assert events[-1]['remaining_seconds'] > 500
