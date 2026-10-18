"""Agents declare behavior; the daemon owns immutable evidence bindings."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest
from coordinare_service_inference.environment.models import EnvironmentManifest

from coordinare.services.environment_discovery import EnvironmentDiscoveryError, discover_and_review
from tests.unit.test_351_environment_discovery import (
    CAPABILITIES,
    COMMIT,
    INVENTORY_DIGEST,
    PATH,
    SOURCE,
    candidate,
)


def declarations(manifest):
    return {key: copy.deepcopy(manifest[key]) for key in
            ('requirements', 'install', 'checks', 'activation', 'services', 'empty_reason')}


class Agent:
    def __init__(self, *, provenance=False, source=SOURCE):
        self.messages = []
        self.provenance = provenance
        self.source = source

    async def __call__(self, messages):
        self.messages.append(copy.deepcopy(messages))
        context = json.loads(messages[1]['content'])
        if len(messages) == 2:
            return json.dumps({'tool': 'read_file', 'path': PATH})
        if 'manifest' in context:
            return json.dumps({'assessment': {'verdict': 'accept', 'rationale': 'Required checks cover cited evidence', 'sources': [PATH]}})
        value = declarations(candidate())
        if self.provenance:
            self.provenance = False
            value['source_commit'] = 'b' * 40
        return json.dumps({'manifest': value})


async def discover(agent, **options):
    async def fetch(path):
        return agent.source
    return await discover_and_review(symphony_name='suite', commit=COMMIT, paths=[PATH, 'other/evidence'],
        capabilities=CAPABILITIES, fetch=fetch, chat=agent, **options)


@pytest.mark.asyncio
async def test_declarations_bind_to_daemon_source_and_independent_read_ledger():
    agent = Agent()
    manifest, review = await discover(agent)
    assert manifest.source_commit == review.source_commit == COMMIT
    assert manifest.inventory_digest == review.inventory_digest == INVENTORY_DIGEST
    assert manifest.capabilities_digest == review.capabilities_digest == CAPABILITIES.digest()
    assert manifest.inputs == review.inputs == {PATH: hashlib.sha256(SOURCE.encode()).hexdigest()}
    assert review.manifest_digest == manifest.digest()
    assert len(agent.messages[2]) == 2
    assert 'different absolute' in json.loads(agent.messages[0][1]['content'])['execution_rules']


@pytest.mark.asyncio
async def test_supplied_provenance_is_rejected_with_feedback_before_valid_proposal():
    agent = Agent(provenance=True)
    events = []
    manifest, _ = await discover(agent, diagnostics=events.append)
    assert manifest.source_commit == COMMIT
    assert events[-1]['repairs'] == 1
    errors = json.loads(agent.messages[2][-1]['content'])['validation_errors']
    assert any(error['code'] == 'extra_forbidden' for error in errors)


@pytest.mark.asyncio
async def test_actual_source_drift_between_independent_reads_still_holds():
    agent = Agent()
    reads = 0
    async def changing(path):
        nonlocal reads
        reads += 1
        return SOURCE if reads == 1 else SOURCE + 'changed'
    with pytest.raises(EnvironmentDiscoveryError, match='review evidence hash changed'):
        await discover_and_review(symphony_name='suite', commit=COMMIT, paths=[PATH, 'other/evidence'],
            capabilities=CAPABILITIES, fetch=changing, chat=agent)


@pytest.mark.asyncio
async def test_prior_required_scope_cannot_be_narrowed_for_executable_repair():
    agent = Agent()
    previous = candidate()
    previous['checks'][0]['scope'] = ['implementer']
    with pytest.raises(EnvironmentDiscoveryError, match='obligation'):
        await discover(agent, previous_manifest=EnvironmentManifest.model_validate(previous),
            execution_feedback={'results': [{'id': 'version', 'status': 'fail', 'exit_code': 2}]})


@pytest.mark.parametrize('change', ['start_id', 'scope', 'init_removed', 'start_optional'])
def test_required_service_lifecycle_cannot_be_waived_in_a_revision(change):
    from coordinare.services.environment_discovery import _preserve_obligations

    data = candidate()
    data['services'] = [{'id': 'background', 'required': True,
        **{phase: {'id': phase, 'argv': ['true']} for phase in ('init', 'start', 'health', 'stop')}}]
    previous = EnvironmentManifest.model_validate(data)
    revised = copy.deepcopy(data)
    service = revised['services'][0]
    if change == 'start_id':
        service['start']['id'] = 'replacement-start'
    elif change == 'scope':
        for phase in ('init', 'start', 'health', 'stop'):
            service[phase]['scope'] = ['qa']
    elif change == 'init_removed':
        service['init'] = None
    else:
        service['start']['required'] = False
    with pytest.raises(EnvironmentDiscoveryError, match='obligation'):
        _preserve_obligations(EnvironmentManifest.model_validate(revised), previous)
