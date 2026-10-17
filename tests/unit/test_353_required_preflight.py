"""Required preflight cannot be skipped; harmless review envelopes need no repair."""
from __future__ import annotations

import json

import pytest

from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_discovery import discover_and_review
from tests.unit.test_353_discovery_repair import (
    CAPABILITIES,
    COMMIT,
    PATH,
    SOURCE,
    assessment,
    candidate,
)
from tests.unit.test_353_environment_probes import report_data, request_data


@pytest.mark.asyncio
async def test_bare_valid_review_can_revise_after_schema_budget_is_exhausted():
    budget = AttemptBudget(schema_repairs=2)
    reviews = 0
    async def fetch(path):
        return SOURCE
    async def chat(messages):
        nonlocal reviews
        context = json.loads(messages[1]['content'])
        if 'manifest' in context:
            reviews += 1
            return json.dumps(assessment(candidate(), 'revise' if reviews == 1 else 'accept')['assessment'])
        if len(messages) == 2:
            return json.dumps({'tool':'read_file', 'path':PATH})
        return json.dumps({'manifest':candidate()})
    _, review = await discover_and_review(symphony_name='suite', commit=COMMIT, paths=[PATH], fetch=fetch,
        chat=chat, capabilities=CAPABILITIES, budget=budget)
    assert review.verdict == 'accept'
    assert reviews == 2
    assert budget.schema_repairs == 2 and budget.coverage_revisions == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('extra', ['source_commit', 'tool'])
async def test_bare_review_with_extra_authority_or_tool_is_not_normalized(extra):
    async def fetch(path):
        return SOURCE
    async def chat(messages):
        if 'manifest' in json.loads(messages[1]['content']):
            return json.dumps({**assessment(candidate())['assessment'],extra:'untrusted'})
        return json.dumps({'tool':'read_file','path':PATH}) if len(messages)==2 else json.dumps({'manifest':candidate()})
    with pytest.raises(EnvironmentDiscoveryError):
        await discover_and_review(symphony_name='suite', commit=COMMIT, paths=[PATH], fetch=fetch, chat=chat,
            capabilities=CAPABILITIES, budget=AttemptBudget(max_schema_repairs=0))


@pytest.mark.asyncio
async def test_manifest_cannot_skip_required_preflight():
    budget = AttemptBudget()
    async def fetch(path):
        return SOURCE
    async def execute(plan):
        pytest.fail('agent did not submit a probe plan')
    async def chat(messages):
        assert 'manifest' not in json.loads(messages[1]['content']), 'must not reach independent review'
        if len(messages)==2:
            return json.dumps({'tool':'read_file','path':PATH})
        return json.dumps({'manifest':candidate()})
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        await discover_and_review(symphony_name='suite', commit=COMMIT, paths=[PATH], fetch=fetch, chat=chat,
            capabilities=CAPABILITIES, budget=budget, probe_executor=execute, require_preflight=True)
    assert exc.value.code == 'preflight_required'
    assert budget.schema_repairs == budget.probe_batches == 0


@pytest.mark.asyncio
async def test_required_preflight_runs_before_manifest_and_fresh_review():
    from coordinare.services.environment_probes import ProbeReport
    budget = AttemptBudget()
    executed = []
    async def fetch(path):
        return SOURCE
    async def execute(plan):
        executed.append(plan.digest())
        return ProbeReport.model_validate(report_data(plan))
    async def chat(messages):
        context = json.loads(messages[1]['content'])
        if 'manifest' in context:
            assert executed and context['probe_evidence']
            return json.dumps(assessment(candidate())['assessment'])
        if len(messages)==2:
            assert context['preflight_required'] is True
            return json.dumps({'tool':'read_file','path':PATH})
        last = json.loads(messages[-1]['content'])
        if last.get('preflight_required'):
            return json.dumps({'probe':request_data()})
        return json.dumps({'manifest':candidate()})
    manifest, _ = await discover_and_review(symphony_name='suite', commit=COMMIT, paths=[PATH], fetch=fetch, chat=chat,
        capabilities=CAPABILITIES, budget=budget, probe_executor=execute, require_preflight=True)
    assert budget.probe_batches == 1
    assert manifest.checks[0].required


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['image_ref', 'platform', 'source_commit', 'cleanup_confirmed'])
async def test_saved_probe_observation_cannot_bypass_current_target(change):
    data = report_data()
    data[change] = False if change == 'cleanup_confirmed' else 'changed'
    budget = AttemptBudget(probe_batches=1, probe_reports=[data])
    async def fetch(path):
        pytest.fail('stale observation must hold before source/model calls')
    async def chat(messages):
        pytest.fail('stale observation must hold before source/model calls')
    with pytest.raises(EnvironmentDiscoveryError):
        await discover_and_review(symphony_name='suite', commit=COMMIT, paths=[PATH], fetch=fetch, chat=chat,
            capabilities=CAPABILITIES, budget=budget, require_preflight=True)
