"""Opt-in product-adapter proof; a skip is unavailable evidence, never readiness."""
from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.timeout(1860)]


def runtime_image():
    if os.environ.get('COORDINARE_RUNTIME_INTEGRATION') != '1':
        pytest.skip('explicit private-runtime consumer integration opt-in required')
    image = os.environ.get('COORDINARE_RUNTIME_CONSUMER_IMAGE')
    assert image and re.fullmatch(r'(?:sha256:[0-9a-f]{64}|[^\s@]+@sha256:[0-9a-f]{64})', image), 'an immutable, explicitly selected runtime image is required'
    return image


def test_packaged_platform_bundle_is_measured_from_actual_files():
    image = runtime_image()
    result = subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--cap-drop', 'ALL',
                             '--security-opt', 'no-new-privileges', '--read-only', '--memory', '128m',
                             '--entrypoint', 'python', image, '-m',
                             'coordinare_service_inference.environment.runtime_bundle'],
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    measured = json.loads(result.stdout)
    assert measured['guest_platform'] == 'linux/x86_64'
    for key in ('guest_base_sha256', 'emulator_sha256', 'firmware_sha256', 'configuration_sha256'):
        assert len(measured[key]) == 64


@pytest.mark.parametrize('network', ['none', 'bridge'])
def test_outer_network_admission_checks_real_container_connectivity(network):
    image = runtime_image()
    program = ('from coordinare_service_inference.environment.runtime_network import require_isolated_outer_network;'
               'require_isolated_outer_network();print("isolated")')
    result = subprocess.run(['docker', 'run', '--rm', '--network', network, '--read-only', '--cap-drop', 'ALL',
                             '--security-opt', 'no-new-privileges', '--entrypoint', 'python', image, '-c', program],
                            capture_output=True, text=True, timeout=30)
    if network == 'none':
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == 'isolated'
    else:
        assert result.returncode != 0
        assert 'private runtime consumer requires isolated outer network' in result.stderr


def test_real_performer_retains_and_releases_private_service_runtime(tmp_path):
    import hashlib
    import shutil
    import time
    import uuid
    from pathlib import Path

    from coordinare_service_inference.environment.execution_source import (
        SourceEntry,
        source_identity,
    )
    from coordinare_service_inference.environment.findings import FindingLedger, FindingReview
    from coordinare_service_inference.environment.models import (
        EnvironmentManifest,
        ExecutionCapabilities,
    )
    from coordinare_service_inference.environment.runtime import RuntimePolicy, RuntimeSpec

    image = runtime_image()
    repo_root = Path(__file__).resolve().parents[2]
    fixture = tmp_path/'fixture'
    shutil.copytree(repo_root/'tests/fixtures/private_service_runtime', fixture, symlinks=True)
    (fixture/'binary.bin').write_bytes(b'\x00\xffbinary\x00runtime\xfe')
    def git(*args):
        return subprocess.check_output(['/usr/bin/git', '-C', str(fixture), '-c', 'core.hooksPath=/dev/null',
            '-c', 'commit.gpgsign=false', '-c', 'user.name=Runtime fixture', '-c', 'user.email=fixture@example.invalid', *args],
            env={'PATH': os.defpath, 'HOME': '/nonexistent', 'GIT_CONFIG_NOSYSTEM': '1',
                 'GIT_AUTHOR_DATE': '2026-01-01T00:00:00Z', 'GIT_COMMITTER_DATE': '2026-01-01T00:00:00Z'}, timeout=10)
    git('init')
    git('add', '--all')
    git('commit', '-m', 'Synthetic runtime fixture')
    commit = git('rev-parse', 'HEAD').decode().strip()
    entries = []
    for raw in git('ls-tree', '-rzl', '--full-tree', 'HEAD').split(b'\0'):
        if raw:
            fields, path = raw.split(b'\t', 1)
            mode, _, oid, size = fields.decode().split()
            entries.append(SourceEntry(path=path.decode(), mode=mode, object_id=oid, size=int(size)))
    source = source_identity(commit, entries)
    measurement = subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges', '--entrypoint', 'python', image,
        '-m', 'coordinare_service_inference.environment.runtime_bundle'], capture_output=True, text=True, timeout=45)
    assert measurement.returncode == 0, measurement.stderr
    measured = json.loads(measurement.stdout)
    info = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image], text=True, timeout=10))[0]
    platform = info['Os']+'/'+{'arm64': 'aarch64', 'amd64': 'x86_64'}.get(info['Architecture'], info['Architecture'])
    policy = RuntimePolicy.model_validate({'schema_version': 1, 'kind': 'private_service_guest',
        'identity': {'schema_version': 1, 'carrier_image': image, 'carrier_platform': platform, **measured},
        'resources': {'cpus': 2, 'memory_mib': 4096, 'cache_bytes': 8*1024**3, 'scratch_bytes': 16*1024**3,
                      'reserve_bytes': 16*1024**3, 'max_seconds': 1800}, 'control_port': 2222, 'max_endpoints': 16})
    spec = RuntimeSpec(schema_version=1, policy=policy,
        endpoints=[{'id': 'fixture-http', 'bind_address': '127.0.0.1', 'guest_port': 8080, 'outer_port': 8080}])
    capability = ExecutionCapabilities(schema_version=2, runtime=policy, image_ref=image, platform=platform,
        cache_mount_path='/cache', install_network='enabled', verify_network='disabled')
    base = 'busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e'
    manifest = EnvironmentManifest.model_validate({'schema_version': 2, 'policy_version': '353-v2',
        'runtime': spec.model_dump(), 'source_commit': commit, 'symphony_name': 'synthetic-runtime',
        'inventory_digest': hashlib.sha256(json.dumps(sorted(item.path for item in entries)).encode()).hexdigest(),
        'capabilities_digest': capability.digest(),
        'inputs': {path: hashlib.sha256((fixture/path).read_bytes()).hexdigest() for path in ('Dockerfile', 'compose.yaml')},
        'requirements': [{'id': 'fixture-service', 'description': 'Synthetic ordinary-client service use',
                          'source': 'compose.yaml', 'checks': ['ordinary-client']}],
        'install': [{'id': 'fixture-pull', 'argv': ['docker', 'pull', base]},
                    {'id': 'fixture-build', 'argv': ['docker', 'build', '--pull=false', '--network=none',
                                                   '-t', 'coordinare-runtime-fixture:local', '{repo}']}],
        'checks': [{'id': 'ordinary-client', 'argv': ['curl', '-fsS', 'http://127.0.0.1:8080/message.txt']}],
        'activation': {'path': [], 'env': {}}, 'empty_reason': None,
        'services': [{'id': 'fixture-compose',
            'start': {'id': 'compose-start', 'argv': ['docker', 'compose', '-f', '{repo}/compose.yaml', 'up', '-d', '--no-build', '--pull', 'never']},
            'health': {'id': 'compose-health', 'argv': ['curl', '-fsS', '--retry', '15', '--retry-all-errors', '--retry-connrefused', '--retry-delay', '1', '--max-time', '20', 'http://127.0.0.1:8080/message.txt']},
            'stop': {'id': 'compose-stop', 'argv': ['docker', 'compose', '-f', '{repo}/compose.yaml', 'down', '--volumes', '--remove-orphans']}}]})
    ledger = FindingLedger(source_commit=commit, reviews=[FindingReview(sequence=1, manifest_digest=manifest.digest(),
                           inputs=manifest.inputs, decisions=[], added_ids=[])])
    assessment = {'schema_version': 2, 'policy_version': '353-findings-v1', 'manifest_digest': manifest.digest(),
        'source_commit': commit, 'symphony_name': 'synthetic-runtime', 'verdict': 'accept',
        'rationale': 'Synthetic protocol fixture only; never live project acceptance', 'sources': list(manifest.inputs),
        'inputs': manifest.inputs, 'inventory_digest': manifest.inventory_digest, 'capabilities_digest': capability.digest(),
        'finding_ledger': ledger.model_dump(), 'finding_ledger_digest': ledger.digest()}
    inputs, cache, evidence = (tmp_path/name for name in ('input', 'cache', 'evidence'))
    for path in (inputs, cache, evidence):
        path.mkdir(mode=0o700)
    shutil.copyfile(repo_root/'tests/runtime_consumer_driver.py', inputs/'driver.py')
    expiry = time.time()+1800
    (inputs/'metadata.json').write_text(json.dumps({'readiness_evidence': False, 'expires_at': expiry, 'contract': {
        'environment_manifest': manifest.model_dump(), 'environment_manifest_digest': manifest.digest(),
        'environment_assessment': assessment, 'environment_execution_source': source.model_dump()}}))
    owned = []
    try:
        for phase in ('installer', 'consumer', 'live_consumer'):
            scratch = tmp_path / ('scratch-' + phase)
            scratch.mkdir(mode=0o700)
            name = 'coordinare-runtime-consumer-'+uuid.uuid4().hex[:12]
            owned.append(name)
            (tmp_path/'ownership.json').write_text(json.dumps({'containers': owned, 'readiness_evidence': False}))
            command = ['docker', 'run', '--name', name, '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                       '--memory', '4g', '--memory-swap', '4g', '--cpus', '2', '--pids-limit', '512',
                       '--network', 'none' if phase == 'consumer' else 'bridge',
                       '--mount', f'type=bind,src={inputs},dst=/input,readonly',
                       '--mount', f'type=bind,src={fixture},dst=/fixture,readonly',
                       '--mount', f'type=bind,src={cache},dst=/cache'+('' if phase == 'installer' else ',readonly'),
                       '--mount', f'type=bind,src={evidence},dst=/evidence',
                       '--mount', f'type=bind,src={scratch},dst=/tmp',
                       '--mount', f'type=bind,src={inputs}/driver.py,dst=/driver.py,readonly',
                       '--mount', f'type=bind,src={repo_root}/tests/runtime_http_consumer_driver.py,dst=/http-driver.py,readonly',
                       '--entrypoint', 'python', image]
            if phase == 'live_consumer':
                from tests.runtime_http_fixture import run_http_consumer

                contract = json.loads((inputs/'metadata.json').read_text())['contract']
                receipt = json.loads((evidence/'installer.json').read_text())['receipt']
                http_evidence = run_http_consumer([*command, '/http-driver.py'], name=name,
                    metadata=contract, receipt=receipt, expires_at=expiry)
                (evidence/'http.json').write_text(json.dumps(http_evidence))
            else:
                completed = subprocess.run([*command, '/driver.py', phase], capture_output=True, text=True,
                                           timeout=max(1, expiry-time.time()))
                (evidence/(phase+'.log')).write_text(completed.stdout+'\n'+completed.stderr)
                assert completed.returncode == 0, completed.stderr[-4000:]
        installer = json.loads((evidence/'installer.json').read_text())
        consumer = json.loads((evidence/'consumer.json').read_text())
        live = json.loads((evidence/'live_consumer.json').read_text())
        assert live['runtime_purpose'] == 'live_consumer' and live['clean_admission_refused_live_carrier']
        assert live['manifest_digest'] == manifest.digest() and live['source_digest'] == source.digest()
        assert live['cache_writes_denied'] and all(live['egress_denied'].values())
        assert all(live['cleanup'].values())
        assert installer['manifest_digest'] == consumer['manifest_digest'] == manifest.digest()
        assert installer['source_digest'] == consumer['source_digest'] == source.digest()
        assert installer['source_root'] != consumer['source_root']
        assert consumer['verification']['runtime_phase'] == 'consumer'
        assert consumer['verification']['runtime_cleanup'] is True
        assert all(item['status'] == 'pass' for item in consumer['verification']['results'])
        assert live['cache_writes_denied'] and live['binary_source_verified']
        assert live['scratch_bind'] == 'scratch-through-runtime\n'
        artifact = installer['receipt']['artifacts'][0]
        with (cache/artifact['path']).open('rb') as stream:
            assert hashlib.file_digest(stream, 'sha256').hexdigest() == artifact['sha256']
    finally:
        subprocess.run(['docker', 'rm', '-f', *owned], capture_output=True, timeout=30)
        absent = [subprocess.run(['docker', 'container', 'inspect', name], capture_output=True, timeout=5) for name in owned]
        confirmed = [item.returncode != 0 and b'No such' in item.stderr for item in absent]
        (tmp_path/'cleanup.json').write_text(json.dumps({'containers': owned, 'confirmed_absent': confirmed}))
        assert all(confirmed), 'owned runtime consumer cleanup unconfirmed'


def test_real_preflight_observes_only_the_configured_platform(tmp_path):
    import asyncio
    import hashlib
    import time

    from coordinare_service_inference.environment.models import ExecutionCapabilities
    from coordinare_service_inference.environment.runtime import RuntimePolicy

    from coordinare.services.environment_attempt import AttemptBudget
    from coordinare.services.environment_probe_executor import execute_environment_probe
    from coordinare.services.environment_probes import ProbePlan, ProbeRequest
    from tests.unit.test_353_execution_source_pipeline import bundle_fixture

    image = runtime_image()
    measurement = subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges', '--entrypoint', 'python', image,
        '-m', 'coordinare_service_inference.environment.runtime_bundle'], capture_output=True, text=True, timeout=45)
    assert measurement.returncode == 0, measurement.stderr
    info = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image], text=True, timeout=10))[0]
    platform = info['Os']+'/'+{'arm64': 'aarch64', 'amd64': 'x86_64'}.get(info['Architecture'], info['Architecture'])
    policy = RuntimePolicy.model_validate({'schema_version': 1, 'kind': 'private_service_guest',
        'identity': {'schema_version': 1, 'carrier_image': image, 'carrier_platform': platform, **json.loads(measurement.stdout)},
        'resources': {'cpus': 2, 'memory_mib': 4096, 'cache_bytes': 8*1024**3, 'scratch_bytes': 16*1024**3,
                      'reserve_bytes': 16*1024**3, 'max_seconds': 1800}, 'control_port': 2222, 'max_endpoints': 16})
    capabilities = ExecutionCapabilities(schema_version=2, runtime=policy, image_ref=image, platform=platform,
        cache_mount_path='/cache', install_network='enabled', verify_network='disabled')
    content = b'FROM scratch\n'
    bundle = bundle_fixture(tmp_path, {'Dockerfile': (content, '100644')})
    request = ProbeRequest.model_validate({'schema_version': 1, 'execution_context': 'private_service',
        'commands': [{'id': 'platform', 'argv': ['/bin/sh', '-eu', '-c',
            'printf scratch-probe > "{scratch}/probe-value"; '
            'ssh -p 2222 root@127.0.0.1 cat "{scratch}/probe-value"; printf "\\n"; '
            'docker version --format "{{.Server.Os}}/{{.Server.Arch}}"'],
                      'timeout_seconds': 30}],
        'citations': {'platform': ['Dockerfile']}, 'questions': {'platform': 'Can the explicitly configured service platform answer?'}})
    plan = ProbePlan(request=request, source_commit=bundle.identity.source_commit,
        inventory_digest=hashlib.sha256(b'Dockerfile').hexdigest(), inputs={'Dockerfile': hashlib.sha256(content).hexdigest()},
        capabilities=capabilities, execution_source_digest=bundle.identity.digest())
    name = 'coordinare-probe-'+plan.digest()[:24]
    (tmp_path/'ownership.json').write_text(json.dumps({'containers': [name], 'readiness_evidence': False}))
    (tmp_path/'plan.json').write_text(plan.model_dump_json())
    started = time.monotonic()
    report = asyncio.run(execute_environment_probe(plan, state={'probe_backend': 'docker'}, source_bundle=bundle,
        budget=AttemptBudget(timeout_seconds=120)))
    elapsed = time.monotonic()-started
    (tmp_path/'report.json').write_text(json.dumps({'report': report.model_dump(mode='json'),
        'elapsed_seconds': elapsed, 'readiness_evidence': False}))
    assert elapsed < 120
    assert report.cleanup_confirmed
    assert report.results[0].status == 'completed' and report.results[0].exit_code == 0
    assert report.results[0].output.splitlines() == ['scratch-probe', 'linux/amd64']
    absent = subprocess.run(['docker', 'container', 'inspect', name], capture_output=True, timeout=10)
    assert absent.returncode != 0 and b'No such' in absent.stderr
