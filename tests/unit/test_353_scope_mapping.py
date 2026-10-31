"""Every reviewed source criterion needs required clean executable evidence."""
from __future__ import annotations

import copy

import pytest
from coordinare_service_inference.environment.models import EnvironmentManifest
from pydantic import ValidationError

from tests.unit.test_353_scope_contract import (
    COMMIT,
    Replies,
    criterion,
    proposal,
    review,
    run,
)


async def mapped_manifest():
    scope = await run(Replies([{'tool': 'read_file', 'path': 'guide'}, proposal(criterion()), review()]))
    identifier = scope.definition.criteria[0].id
    data = {'schema_version': 3, 'policy_version': '353-scope-v1', 'symphony_name': 'suite',
        'source_commit': COMMIT, 'inputs': dict(scope.all_inputs), 'inventory_digest': scope.definition.inventory_digest,
        'capabilities_digest': 'c'*64, 'runtime': None,
        'scope_contract': scope.model_dump(mode='json'), 'scope_contract_digest': scope.digest(),
        'criterion_checks': {identifier: ['operational']},
        'requirements': [{'id': 'operational-obligation', 'description': 'Documented environment works',
                          'source': 'guide', 'checks': ['operational'], 'required': True}],
        'install': [], 'checks': [{'id': 'operational', 'argv': ['tool', 'exercise'], 'required': True}],
        'activation': {}, 'services': [], 'empty_reason': None}
    return data, identifier


@pytest.mark.asyncio
async def test_complete_mapping_is_part_of_canonical_identity():
    data, identifier = await mapped_manifest()
    manifest = EnvironmentManifest.model_validate(data)
    assert manifest.scope_contract_digest == manifest.scope_contract.digest()
    changed = copy.deepcopy(data)
    changed['checks'].append({'id': 'second', 'argv': ['tool', 'exercise-again'], 'required': True})
    changed['criterion_checks'][identifier].append('second')
    assert EnvironmentManifest.model_validate(changed).digest() != manifest.digest()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['missing', 'optional', 'install_only', 'invented', 'forged_criterion',
                                     'wrong_digest', 'wrong_inventory', 'wrong_source', 'missing_review_input'])
async def test_incomplete_or_forged_mapping_never_validates(mutation):
    data, identifier = await mapped_manifest()
    if mutation == 'missing':
        data['criterion_checks'] = {}
    elif mutation == 'optional':
        # The same defect as SampleDatabase's required criterion -> optional runtime check.
        data['requirements'] = []
        data['empty_reason'] = 'candidate disclaimer cannot waive source obligation'
        data['checks'][0]['required'] = False
        data['checks'].append({'id': 'irrelevant', 'argv': ['true'], 'required': True})
    elif mutation == 'install_only':
        data['install'] = [{'id': 'installed', 'argv': ['true'], 'required': True}]
        data['criterion_checks'][identifier] = ['installed']
    elif mutation == 'invented':
        data['criterion_checks'][identifier] = ['unlisted']
    elif mutation == 'forged_criterion':
        data['criterion_checks']['S'+'0'*16] = ['operational']
    elif mutation == 'wrong_digest':
        data['scope_contract_digest'] = '0'*64
    elif mutation == 'wrong_inventory':
        data['inventory_digest'] = '0'*64
    elif mutation == 'wrong_source':
        data['source_commit'] = 'b'*40
    elif mutation == 'missing_review_input':
        data['inputs'].pop('guide')
        data['requirements'] = []
        data['empty_reason'] = 'client-only candidate'
    with pytest.raises(ValidationError):
        EnvironmentManifest.model_validate(data)


@pytest.mark.asyncio
async def test_no_legacy_version_can_carry_a_reviewed_scope_as_current():
    data, _ = await mapped_manifest()
    data.update(schema_version=1, policy_version='351-v1')
    with pytest.raises(ValidationError):
        EnvironmentManifest.model_validate(data)


@pytest.mark.asyncio
async def test_no_reviewer_can_turn_unknown_or_out_of_scope_into_required_check_mapping():
    scope = await run(Replies([{'tool': 'read_file', 'path': 'guide'},
        proposal(criterion(applicability='not_applicable')), review()]))
    data, identifier = await mapped_manifest()
    data.update(scope_contract=scope.model_dump(mode='json'), scope_contract_digest=scope.digest())
    data['criterion_checks'] = {scope.definition.criteria[0].id: ['operational']}
    assert identifier != scope.definition.criteria[0].id
    with pytest.raises(ValidationError):
        EnvironmentManifest.model_validate(data)


@pytest.mark.asyncio
async def test_required_service_cannot_optionalize_a_lifecycle_command():
    data, _ = await mapped_manifest()
    data['services'] = [{'id': 'service', 'required': True,
        'start': {'id': 'start', 'argv': ['true'], 'required': False},
        'health': {'id': 'health', 'argv': ['true']}, 'stop': {'id': 'stop', 'argv': ['true']}}]
    with pytest.raises(ValidationError):
        EnvironmentManifest.model_validate(data)


@pytest.mark.asyncio
async def test_obligation_and_command_id_namespaces_cannot_be_ambiguous():
    data, _ = await mapped_manifest()
    data['requirements'][0]['id'] = data['checks'][0]['id']
    with pytest.raises(ValidationError):
        EnvironmentManifest.model_validate(data)


@pytest.mark.asyncio
async def test_mapping_cannot_use_a_role_only_check_for_whole_environment():
    data, _ = await mapped_manifest()
    data['checks'][0]['scope'] = ['qa']
    with pytest.raises(ValidationError):
        EnvironmentManifest.model_validate(data)


@pytest.mark.asyncio
async def test_exact_sampledb_optional_runtime_link_shape_remains_rejected():
    data, identifier = await mapped_manifest()
    data['requirements'] = [{'id': 'backend-admin-rails', 'source': 'guide',
        'description': 'Source-backed operational obligation', 'checks': ['rails-container-runtime-check'], 'required': True}]
    data['checks'] = [{'id': 'rails-container-runtime-check', 'argv': ['true'], 'required': False},
                      {'id': 'baseline', 'argv': ['true'], 'required': True}]
    data['criterion_checks'] = {identifier: ['rails-container-runtime-check']}
    with pytest.raises(ValidationError):
        EnvironmentManifest.model_validate(data)


@pytest.mark.asyncio
async def test_constructor_binds_configured_runtime_policy_and_only_declared_endpoints():
    from coordinare_service_inference.environment.models import (
        EnvironmentManifestProposal,
        ExecutionCapabilities,
    )
    from coordinare_service_inference.environment.runtime import RuntimePolicy
    from coordinare_service_inference.environment.scope import ScopeContract

    from coordinare.services.environment_attempt import AttemptBudget
    from coordinare.services.environment_construction import ReviewedConstruction
    from coordinare.services.environment_discovery import DiscoveryLimits
    from tests.unit.test_353_runtime_identity import native_data, policy_data

    data, _ = await mapped_manifest()
    budget = AttemptBudget()
    budget.scope_contract = ScopeContract.model_validate(data['scope_contract'])
    policy = RuntimePolicy.model_validate(policy_data())
    capabilities = ExecutionCapabilities.model_validate({**native_data(), 'schema_version': 2, 'runtime': policy})
    async def unavailable(*args, **kwargs):
        pytest.fail('constructor admission cannot call a model or execute a command')
    construction = ReviewedConstruction(('suite', COMMIT), ['guide'], unavailable, unavailable,
        DiscoveryLimits(), budget, capabilities)
    declaration = {key: data[key] for key in EnvironmentManifestProposal.model_fields if key in data}
    declaration['runtime_endpoints'] = [{'id': 'local-service', 'outer_port': 15432, 'guest_port': 5432, 'bind_address': '127.0.0.1'}]
    manifest = construction.admit(declaration, data['inputs'])
    assert manifest.runtime.policy == policy
    assert manifest.runtime.endpoints[0].outer_port == 15432
    assert manifest.capabilities_digest == capabilities.digest()
    assert manifest.scope_contract_digest == budget.scope_contract.digest()
    declaration['runtime_endpoints'][0]['outer_port'] = policy.control_port
    with pytest.raises(ValidationError):
        construction.admit(declaration, data['inputs'])


@pytest.mark.asyncio
async def test_repair_cannot_drop_established_criterion_to_required_check_links():
    from coordinare.services.environment_attempt import EnvironmentDiscoveryError
    from coordinare.services.environment_obligations import preserve_obligations

    data, identifier = await mapped_manifest()
    data['checks'].append({'id': 'additional', 'argv': ['true'], 'required': True})
    previous = EnvironmentManifest.model_validate(data)
    changed = copy.deepcopy(data)
    changed['criterion_checks'][identifier] = ['additional']
    current = EnvironmentManifest.model_validate(changed)
    with pytest.raises(EnvironmentDiscoveryError) as failure:
        preserve_obligations(current, previous)
    assert failure.value.code == 'obligation_removed'
