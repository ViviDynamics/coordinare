"""An executable review may ask to release a required criterion that only a per-run network fetch makes unverifiable.

The request reopens scope through the existing reviewed path. The scope reviewer alone can release the criterion, and
obligation preservation honours only the release it accepted, retiring the checks that served nothing else.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment.models import EnvironmentManifest

from coordinare.services import environment_obligations
from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_construction import ScopeReopenedError
from coordinare.services.environment_discovery import _reopen_for_release, release_requests
from coordinare.services.environment_obligations import _released_checks, preserve_obligations
from tests.unit.test_353_scope_mapping import mapped_manifest


def record(category: str, *affected: str) -> SimpleNamespace:
    return SimpleNamespace(id='F0001', proposal=SimpleNamespace(category=category, affected_ids=list(affected)))


def scope(*criteria: tuple[str, str]) -> SimpleNamespace:
    return SimpleNamespace(definition=SimpleNamespace(criteria=[
        SimpleNamespace(id=identifier, applicability=applicability) for identifier, applicability in criteria]))


def manifest(criterion_checks: dict[str, list[str]]) -> SimpleNamespace:
    return SimpleNamespace(criterion_checks=criterion_checks)


def test_a_network_finding_requests_release_of_the_required_criteria_it_names_or_maps_to():
    ledger = SimpleNamespace(records=[record('runtime_network', 'c00-compose-dev'), record('execution_definition', 'c00-x')],
                             open_ids=lambda: ['F0001'])
    criteria = scope(('S1', 'required'), ('S2', 'required'), ('S3', 'not_applicable'))
    mapped = manifest({'S1': ['c00-compose-dev'], 'S2': ['c00-other'], 'S3': ['c00-compose-dev']})
    assert release_requests(ledger, mapped, criteria) == ['S1']
    assert release_requests(ledger, mapped, scope(('S1', 'required'))) == ['S1']


def test_a_release_names_only_criteria_its_finding_reaches_and_only_open_findings():
    criteria = scope(('S1', 'required'), ('S2', 'required'))
    mapped = manifest({'S1': ['check-a'], 'S2': ['check-b']})
    named = SimpleNamespace(records=[record('runtime_network', 'unknown-id')], open_ids=lambda: ['F0001'])
    assert release_requests(named, mapped, criteria) == []
    closed = SimpleNamespace(records=[record('remote_state', 'check-a')], open_ids=list)
    assert release_requests(closed, mapped, criteria) == []
    open_ = SimpleNamespace(records=[record('runtime_network', 'check-a')], open_ids=lambda: ['F0001'])
    assert release_requests(open_, mapped, criteria) == ['S1']
    assert release_requests(SimpleNamespace(records=[], open_ids=list), mapped, criteria) == []


def test_a_release_retires_only_checks_that_serve_no_other_criterion():
    previous = manifest({'S1': ['network-check', 'shared'], 'S2': ['shared', 'offline-check']})
    assert _released_checks(previous, {'S1'}) == {'network-check'}
    assert _released_checks(previous, {'S1', 'S2'}) == {'network-check', 'shared', 'offline-check'}
    assert _released_checks(previous, set()) == set()


@pytest.mark.asyncio
async def test_an_accepted_release_lets_the_network_check_go(monkeypatch):
    data, identifier = await mapped_manifest()
    data['checks'].append({'id': 'network-build', 'argv': ['docker', 'build', '.'], 'required': True})
    data['requirements'][0]['checks'].append('network-build')
    data['criterion_checks'][identifier] = ['operational', 'network-build']
    previous = EnvironmentManifest.model_validate(data)
    released = copy.deepcopy(data)
    released['checks'] = [check for check in released['checks'] if check['id'] != 'network-build']
    released['requirements'][0]['checks'] = ['operational']
    released['criterion_checks'] = {identifier: ['operational']}
    monkeypatch.setattr(environment_obligations, 'released_criteria', lambda _ledger: {identifier})
    preserve_obligations(EnvironmentManifest.model_validate(released), previous)
    # A criterion the scope never released keeps its check: removing that check is still an omission.
    monkeypatch.setattr(environment_obligations, 'released_criteria', lambda _ledger: set())
    with pytest.raises(EnvironmentDiscoveryError) as caught:
        preserve_obligations(EnvironmentManifest.model_validate(released), previous)
    assert caught.value.code == 'obligation_removed'


@pytest.mark.asyncio
async def test_a_release_request_reopens_scope_for_the_reviewer_and_records_the_criteria():
    budget = AttemptBudget()
    budget.scope_contract = SimpleNamespace()
    budget.scope_phase = 'accepted'
    prepared: list[bool] = []

    async def prepare() -> None:
        prepared.append(budget.scope_contract is None and budget.release_requests == ['S1'])

    attempt = SimpleNamespace(budget=budget, construction=SimpleNamespace(prepare=prepare))
    with pytest.raises(ScopeReopenedError):
        await _reopen_for_release(attempt, ['S1'])
    assert prepared == [True]
    assert budget.scope_contract is None and budget.scope_phase == 'review' and budget.release_requests == ['S1']


def test_check_guidance_consumes_install_artifacts_instead_of_rebuilding_them():
    """v44's F0011: a clean-phase check rebuilt the Dockerfile offline rather than inspecting the install-phase image."""
    from coordinare.services.environment_shard_construction import (
        CHECK_AUTHOR_INSTRUCTIONS,
        INSTALL_ARTIFACT_RULE,
    )

    assert INSTALL_ARTIFACT_RULE in CHECK_AUTHOR_INSTRUCTIONS
    assert 'never rebuilds' in INSTALL_ARTIFACT_RULE and 'no network' in INSTALL_ARTIFACT_RULE


@pytest.mark.asyncio
async def test_a_revision_that_releases_a_criterion_routes_the_author_and_reopens_scope(monkeypatch):
    from coordinare.services import environment_discovery as discovery

    budget = AttemptBudget()
    budget.finding_ledger = SimpleNamespace(records=[record('runtime_network', 'check-a')], open_ids=lambda: ['F0001'])
    budget.scope_contract = SimpleNamespace(definition=scope(('S1', 'required')).definition)
    attempt = SimpleNamespace(budget=budget, messages=[], required_ids={}, obligation_ledger=None,
                              prior_manifests=[manifest({'S1': ['check-a']})],
                              construction=SimpleNamespace(require_scope=lambda: budget.scope_contract))
    routed, reopened = [], []
    monkeypatch.setattr(discovery, '_require_revision', lambda _assessment: None)
    monkeypatch.setattr(discovery, '_append_coverage_revision', lambda *_args: routed.append(True))

    async def reopen(_attempt, criteria):
        reopened.append(criteria)

    monkeypatch.setattr(discovery, '_reopen_for_release', reopen)
    await discovery._revise(attempt, None, {}, SimpleNamespace())
    assert routed == [True] and reopened == [['S1']]


@pytest.mark.asyncio
async def test_each_rejected_executable_round_costs_one_coverage_revision():
    """v44 spent eight revisions: four rejected scope rounds and four rejected executable rounds, the fifth executable
    rejection then found the allowance spent. A round is charged once however many parts it has."""
    from coordinare.services import environment_discovery as discovery

    budget = AttemptBudget()
    ledger = SimpleNamespace(records=[], open_ids=list)
    revise = SimpleNamespace(verdict='revise', finding_ledger=ledger)
    await discovery._checkpoint_finding_review(budget, revise, {'capability_unavailable': False})
    assert budget.coverage_revisions == 1
    accept = SimpleNamespace(verdict='accept', finding_ledger=ledger)
    await discovery._checkpoint_finding_review(budget, accept, {'capability_unavailable': False})
    assert budget.coverage_revisions == 1
