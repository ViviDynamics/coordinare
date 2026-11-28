"""Debug resume from a saved accepted contract: only the runtime binding moves, and every other identity must match.

Synthetic fixtures only; saved acceptance runs are never read here.
"""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from agent.performer.tests.source_contract_fixture import (
    current_manifest_fixture,
    current_review_fixture,
)
from coordinare_service_inference.environment.models import (
    CoverageAssessment,
    EnvironmentManifest,
    ExecutionCapabilities,
)
from coordinare_service_inference.environment.runtime import RuntimePolicy, RuntimeSpec
from scripts.environment_validation_resume import (
    ResumeRefusedError,
    load_source,
    rebind,
    resumed_from_record,
)

from coordinare.services.env_cache import ContractTarget, SavedContract
from coordinare.services.environment_acceptance import accepted_environment
from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_repair import journal_for
from coordinare.services.environment_runtime_evidence import require_runtime_target
from coordinare.services.environment_scope_reuse import inventory_digest
from tests.unit.env_manifest_fixture import fixture_execution_source, reviewed_metadata
from tests.unit.test_353_bootstrap_pipeline import COMMIT, SOURCE, Repository, setup
from tests.unit.test_353_runtime_identity import native_data, policy_data

OLD_IMAGE = 'registry.example/performer@sha256:' + 'a' * 64
NEW_IMAGE = '127.0.0.1:61000/coordinare-env-validation-0123456789ab@sha256:' + '9' * 64
BINDING_FIELDS = ['manifest.runtime.policy', 'manifest.capabilities_digest', 'assessment.capabilities_digest',
                  'assessment.manifest_digest', 'assessment.finding_ledger.reviews[-1].manifest_digest',
                  'assessment.finding_ledger_digest']


def runtime_capabilities(image: str = OLD_IMAGE, *, mount: str = '/devenv/suite',
                         guest: str = 'linux/x86_64') -> ExecutionCapabilities:
    policy = policy_data()
    policy['identity'] = {**policy['identity'], 'carrier_image': image, 'guest_platform': guest}
    return ExecutionCapabilities.model_validate({**native_data(), 'image_ref': image, 'cache_mount_path': mount,
        'schema_version': 2, 'runtime': RuntimePolicy.model_validate(policy)})


def native_capabilities(image: str = OLD_IMAGE, *, mount: str = '/devenv/suite') -> ExecutionCapabilities:
    return ExecutionCapabilities.model_validate({**native_data(), 'image_ref': image, 'cache_mount_path': mount})


def saved_contract(capabilities: ExecutionCapabilities, *, commit: str = COMMIT,
                   paths: tuple[str, ...] = ('guide',)) -> tuple[EnvironmentManifest, CoverageAssessment]:
    """An accepted contract bound to the pipeline fixture repository and the given target."""
    inputs = {'guide': hashlib.sha256(SOURCE.encode()).hexdigest()}
    metadata = reviewed_metadata(capabilities=capabilities, inputs=inputs, source_commit=commit, symphony_name='suite',
        install=[{'id': 'install', 'argv': ['true']}], empty_reason='Synthetic resume fixture')
    data = {**metadata['environment_manifest'], 'inventory_digest': inventory_digest(paths)}
    if capabilities.runtime is not None:
        data['runtime'] = RuntimeSpec(schema_version=1, policy=capabilities.runtime, endpoints=[]).model_dump(mode='json')
    manifest = current_manifest_fixture(data)
    assessment = CoverageAssessment.model_validate(current_review_fixture(manifest, {
        **metadata['environment_assessment'], 'manifest_digest': manifest.digest(),
        'inventory_digest': manifest.inventory_digest}))
    return manifest, assessment


def acceptance_cache(tmp_path, manifest, assessment, capabilities):
    _service, cache, _state, _capability = setup(tmp_path)
    cache.environment_manifest = manifest.model_dump(mode='json')
    cache.environment_assessment = assessment.model_dump(mode='json')
    cache.environment_capabilities = capabilities.model_dump(mode='json')
    return cache


def test_rebind_changes_only_binding_fields_and_passes_acceptance(tmp_path):
    manifest, assessment = saved_contract(runtime_capabilities())
    target = runtime_capabilities(NEW_IMAGE, mount='/devenv/suite-r1')
    rebound, review, changed = rebind(manifest, assessment, target)
    assert changed == BINDING_FIELDS
    assert rebound.runtime is not None and rebound.runtime.policy == target.runtime
    assert rebound.runtime.endpoints == manifest.runtime.endpoints
    assert rebound.capabilities_digest == target.digest() == review.capabilities_digest
    assert review.manifest_digest == rebound.digest() == review.finding_ledger.reviews[-1].manifest_digest
    exclude = {'runtime', 'capabilities_digest'}
    assert rebound.model_dump(mode='json', exclude=exclude) == manifest.model_dump(mode='json', exclude=exclude)
    exclude = {'capabilities_digest', 'manifest_digest', 'finding_ledger', 'finding_ledger_digest'}
    assert review.model_dump(mode='json', exclude=exclude) == assessment.model_dump(mode='json', exclude=exclude)
    cache = acceptance_cache(tmp_path, rebound, review, target)
    assert accepted_environment(cache) is not None
    require_runtime_target(rebound, cache)
    # The saved contract itself is not accepted against the new target: the binding really moved.
    assert rebound.digest() != manifest.digest()
    (tmp_path / 'saved').mkdir()
    with pytest.raises(ValueError, match='policy differs'):
        require_runtime_target(manifest, acceptance_cache(tmp_path / 'saved', manifest, assessment, target))


def test_rebind_is_pure_and_records_nothing_for_an_unchanged_native_target():
    manifest, assessment = saved_contract(native_capabilities())
    before = (manifest.model_dump(mode='json'), assessment.model_dump(mode='json'))
    rebound, review, changed = rebind(manifest, assessment, native_capabilities())
    assert changed == []
    assert (rebound.digest(), review.manifest_digest) == (manifest.digest(), assessment.manifest_digest)
    assert (manifest.model_dump(mode='json'), assessment.model_dump(mode='json')) == before
    _, _, changed = rebind(manifest, assessment, native_capabilities(NEW_IMAGE))
    assert 'manifest.runtime.policy' not in changed and 'manifest.capabilities_digest' in changed


@pytest.mark.parametrize('target', [
    pytest.param(lambda: native_capabilities(NEW_IMAGE), id='runtime_removed'),
    pytest.param(lambda: runtime_capabilities(NEW_IMAGE, guest='linux/aarch64'), id='other_guest'),
])
def test_rebind_refuses_a_target_of_another_kind(target):
    manifest, assessment = saved_contract(runtime_capabilities())
    with pytest.raises(ResumeRefusedError):
        rebind(manifest, assessment, target())


def test_rebind_refuses_an_assessment_that_is_not_accepted():
    manifest, assessment = saved_contract(native_capabilities())
    revise = CoverageAssessment.model_validate({**assessment.model_dump(mode='json'), 'verdict': 'revise'})
    with pytest.raises(ResumeRefusedError, match='does not accept'):
        rebind(manifest, revise, native_capabilities(NEW_IMAGE))
    other = CoverageAssessment.model_validate({**assessment.model_dump(mode='json'), 'symphony_name': 'another'})
    with pytest.raises(ResumeRefusedError):
        rebind(manifest, other, native_capabilities(NEW_IMAGE))


def write_source(root, manifest, assessment, *, attempt=0, finished=True, execution_source=None, repo='suite'):
    directory = root / repo / f'attempt-{attempt:03}'
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'accepted-manifest.json').write_text(json.dumps(manifest))
    (directory / 'accepted-assessment.json').write_text(json.dumps(assessment))
    source = execution_source or fixture_execution_source(COMMIT, files={'guide': SOURCE}).model_dump(mode='json')
    (directory / 'execution-source.json').write_text(json.dumps(source))
    (directory / 'execution-capabilities.json').write_text(json.dumps(native_capabilities().model_dump(mode='json')))
    if finished:
        (root / repo / 'result.json').write_text(json.dumps({'status': 'held'}))
    return directory


def dumps(manifest, assessment):
    return manifest.model_dump(mode='json'), assessment.model_dump(mode='json')


def test_load_source_selects_the_last_accepted_attempt_by_default(tmp_path):
    manifest, assessment = saved_contract(native_capabilities())
    write_source(tmp_path, *dumps(manifest, assessment), attempt=0)
    write_source(tmp_path, *dumps(manifest, assessment), attempt=1)
    partial = tmp_path / 'suite' / 'attempt-002'
    partial.mkdir()
    (partial / 'accepted-manifest.json').write_text(json.dumps(manifest.model_dump(mode='json')))
    (tmp_path / 'suite' / 'attempt-003').mkdir()
    link = tmp_path / 'link'
    link.symlink_to(tmp_path)
    source = load_source(link, 'suite')
    assert source.attempt == 1 and source.attempt_dir == tmp_path.resolve() / 'suite' / 'attempt-001'
    assert source.manifest.digest() == manifest.digest()
    assert source.capabilities == native_capabilities().model_dump(mode='json')
    assert load_source(tmp_path, 'suite', 0).attempt == 0
    with pytest.raises(ResumeRefusedError, match='attempt-002'):
        load_source(tmp_path, 'suite', 2)
    rebound, review, changed = rebind(source.manifest, source.assessment, native_capabilities(NEW_IMAGE))
    record = resumed_from_record(source, manifest=rebound, assessment=review,
        capabilities=native_capabilities(NEW_IMAGE).model_dump(mode='json'), changed=changed)
    assert record['attempt'] == 1 and record['source_commit'] == COMMIT
    assert record['original']['manifest_digest'] == manifest.digest() != record['rebound']['manifest_digest']
    assert record['changed_binding_fields'] == changed


def test_load_source_refuses_an_unfinished_run(tmp_path):
    manifest, assessment = saved_contract(native_capabilities())
    write_source(tmp_path, *dumps(manifest, assessment), finished=False)
    with pytest.raises(ResumeRefusedError, match=r'result\.json'):
        load_source(tmp_path, 'suite')


@pytest.mark.parametrize('damage', ['manifest_schema', 'assessment_not_accepted', 'execution_source_commit'])
def test_load_source_refuses_an_invalid_or_unaccepted_contract(tmp_path, damage):
    manifest_data, assessment_data = dumps(*saved_contract(native_capabilities()))
    execution_source = None
    if damage == 'manifest_schema':
        manifest_data['schema_version'] = 9
    elif damage == 'assessment_not_accepted':
        assessment_data['verdict'] = 'revise'
    else:
        execution_source = fixture_execution_source('b' * 40).model_dump(mode='json')
    write_source(tmp_path, manifest_data, assessment_data, execution_source=execution_source)
    with pytest.raises(ResumeRefusedError):
        load_source(tmp_path, 'suite')


async def bundle_identity(tmp_path, repository):
    bundle = await repository.prepare_execution_source('org', 'suite', commit=COMMIT,
        destination=tmp_path / 'identity' / 'payload', deadline=asyncio.get_running_loop().time() + 60)
    return bundle.identity.model_dump(mode='json')


def contract_target(cache, repository, capabilities):
    return ContractTarget(symphony_name='suite', repo='suite', github_org='org', github_service=repository,
        cache_state=cache, cache_dir=cache.cache_dir, cache_mount_path=capabilities.cache_mount_path,
        performer_mounts=(cache.cache_dir.parent,))


async def adopt(tmp_path, *, manifest, assessment, repository=None, source_execution=None):
    service, cache, state, _ = setup(tmp_path)
    repository = repository or Repository()
    target = runtime_capabilities(NEW_IMAGE, mount='/devenv/suite')
    service.bind_kubernetes_state(state)
    saved = SavedContract(manifest=manifest, assessment=assessment,
                          source_execution=source_execution or await bundle_identity(tmp_path, Repository()))
    changed = await service.adopt_accepted_artifacts(contract_target(cache, repository, target), saved, rebind,
                                                     capabilities=target)
    return cache, changed


@pytest.mark.asyncio
async def test_adopt_publishes_the_rebound_contract_as_executing_without_a_model(tmp_path):
    manifest, assessment = saved_contract(runtime_capabilities())
    cache, changed = await adopt(tmp_path, manifest=manifest, assessment=assessment)
    assert changed == BINDING_FIELDS
    accepted = accepted_environment(cache)
    assert accepted is not None and accepted.runtime.policy.identity.carrier_image == NEW_IMAGE
    assert accepted.capabilities_digest == ExecutionCapabilities.model_validate(cache.environment_capabilities).digest()
    journal = journal_for(cache)
    assert journal.status == 'executing' and journal.calls == 0
    assert journal.coverage_manifest is not None and journal.coverage_manifest.digest() == accepted.digest()
    assert {path.name for path in cache.cache_dir.iterdir()} >= {'manifest.json', 'activate.sh', 'verify.sh'}


@pytest.mark.parametrize('mismatch', ['commit', 'inventory', 'execution_source'])
@pytest.mark.asyncio
async def test_adopt_refuses_a_different_commit_inventory_or_execution_source(tmp_path, mismatch):
    manifest, assessment = saved_contract(runtime_capabilities(), commit='b' * 40 if mismatch == 'commit' else COMMIT)
    repository = Repository()
    source_execution = None
    if mismatch == 'inventory':
        repository.paths = ['guide', 'notes']
    elif mismatch == 'execution_source':
        source_execution = fixture_execution_source(COMMIT, files={'guide': 'another tree'}).model_dump(mode='json')
    with pytest.raises(EnvironmentDiscoveryError) as raised:
        await adopt(tmp_path, manifest=manifest, assessment=assessment, repository=repository,
                    source_execution=source_execution)
    assert raised.value.code == 'resume_source_mismatch'
    assert {'commit': 'commit', 'inventory': 'inventory', 'execution_source': 'execution source'}[mismatch] in str(raised.value)


@pytest.mark.asyncio
async def test_adopt_refuses_a_contract_the_rebinder_cannot_admit(tmp_path):
    manifest, assessment = saved_contract(runtime_capabilities())

    def tampering(manifest, assessment, capabilities):
        rebound, review, changed = rebind(manifest, assessment, capabilities)
        return rebound, CoverageAssessment.model_validate({**review.model_dump(mode='json'),
                                                           'capabilities_digest': 'f' * 64}), changed

    service, cache, state, _ = setup(tmp_path)
    service.bind_kubernetes_state(state)
    target = runtime_capabilities(NEW_IMAGE)
    saved = SavedContract(manifest=manifest, assessment=assessment,
                          source_execution=await bundle_identity(tmp_path, Repository()))
    with pytest.raises(EnvironmentDiscoveryError) as raised:
        await service.adopt_accepted_artifacts(contract_target(cache, Repository(), target), saved, tampering,
                                               capabilities=target)
    assert raised.value.code == 'resume_rebind_refused'
    assert cache.environment_manifest is None and journal_for(cache).status != 'executing'
