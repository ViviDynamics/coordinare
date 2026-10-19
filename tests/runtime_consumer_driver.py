"""Synthetic product-adapter execution inside an owned integration container."""
from __future__ import annotations

import asyncio
import hashlib
import http.server
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from coordinare_service_inference.environment.execution_source import ExecutionSourceIdentity
from coordinare_service_inference.environment.execution_source_checkout import checkout_identity
from performer.backends._env_policy import build_subprocess_env
from performer.environment_contract import (
    accepted_manifest,
    close_environment,
    execute_contract,
    prepare_environment,
)
from performer.models import Score, Stand


class Canary(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'carrier-only\n')
    def log_message(self, *args):
        pass


async def run(phase: str | None = None, *, contract: dict | None = None) -> None:
    phase = phase or sys.argv[1]
    data = json.loads(Path('/input/metadata.json').read_text())
    source = Path('/work/source-a' if phase == 'installer' else '/work/relocated/source-'+phase)
    source.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree('/fixture', source, symlinks=True)
    score = Score(title='Synthetic runtime integration; never project readiness', role='env_bootstrap' if phase == 'installer' else 'implementing',
                  repo_url='https://example.invalid/fixtures/synthetic-runtime', branch='main', env_cache_path='/cache',
                  environment_attempt_expires_at=data['expires_at'], **(contract if contract is not None else data['contract']))
    if phase != 'installer' and score.environment_runtime_artifacts is None:
        installer = json.loads(Path('/evidence/installer.json').read_text())
        score.environment_runtime_artifacts = installer['receipt']
    stand = Stand(path=source, branch='main')
    actual = await checkout_identity(source, commit=score.environment_manifest['source_commit'],
                                     review_inputs=score.environment_manifest['inputs'])
    expected = ExecutionSourceIdentity.model_validate(score.environment_execution_source)
    assert actual == expected
    result = {'readiness_evidence': False, 'phase': phase, 'manifest_digest': score.environment_manifest_digest,
              'source_digest': actual.digest(), 'source_root': str(source)}
    if phase == 'consumer':
        from coordinare_service_inference.environment.runner import run_manifest
        from coordinare_service_inference.environment.runtime import RuntimeArtifactReceipt

        try:
            with tempfile.TemporaryDirectory(prefix='clean-verifier-') as scratch:
                report = await run_manifest(accepted_manifest(score), cache=Path('/cache'), repo=source,
                    scratch=Path(scratch), install=False, scope='', execution_source_digest=actual.digest(),
                    runtime_artifacts=RuntimeArtifactReceipt.model_validate(score.environment_runtime_artifacts),
                    runtime_deadline=time.monotonic()+data['expires_at']-time.time())
            result['verification'] = report.model_dump(mode='json')
            assert report.passed and report.runtime_phase == 'consumer' and report.runtime_cleanup is True
            assert report.manifest_digest == score.environment_manifest_digest
            assert report.execution_source_digest == actual.digest()
        finally:
            Path('/evidence/consumer.json').write_text(json.dumps(result, indent=2)+'\n')
        return
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 18081), Canary)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    lease = None
    try:
        if phase == 'live_consumer':
            from coordinare_service_inference.environment.models import EnvironmentManifest
            from coordinare_service_inference.environment.runtime import RuntimeArtifactReceipt
            from coordinare_service_inference.environment.runtime_supervisor import open_runtime

            manifest = EnvironmentManifest.model_validate(score.environment_manifest)
            with tempfile.TemporaryDirectory(prefix='clean-network-admission-') as scratch:
                try:
                    candidate = await open_runtime(manifest.runtime,
                        artifacts=RuntimeArtifactReceipt.model_validate(score.environment_runtime_artifacts),
                        repo=stand.path, cache=Path('/cache'), scratch=Path(scratch), install=False,
                        deadline=time.monotonic()+data['expires_at']-time.time(),
                        manifest_digest=manifest.digest(), source_digest=actual.digest())
                except ValueError as error:
                    assert 'isolated outer network' in str(error)
                    result['clean_admission_refused_live_carrier'] = True
                else:
                    await candidate.close(deadline=candidate.deadline)
                    raise AssertionError('clean verification admitted the bridged live-job carrier')
        await prepare_environment(score, stand)
        lease = stand.environment_runtime
        assert lease is not None
        result['runtime_purpose'] = lease.purpose
        code, key_paths = await lease.ssh("find /etc/ssh -name 'ssh_host_*_key'", deadline=lease.deadline, check=False)
        result['persistent_key_inspection'] = {'exit_code': code, 'paths': key_paths.decode()}
        _, startup = await lease.ssh("ls -l /etc/runlevels/*/sshd 2>/dev/null; ps -ef; grep -nE 'ssh-keygen|generate|start_pre|need|after' /etc/init.d/sshd /etc/init.d/sshd-keygen /etc/cloud/cloud.cfg 2>/dev/null; grep -E 'cc_ssh|ssh-keygen|Generating|sshd' /var/log/cloud-init.log | tail -n 12", deadline=lease.deadline, check=False)
        result['platform_startup_diagnostic'] = startup.decode()[-16000:]
        assert code == 0, 'private host-key inspection SSH command failed'
        assert not key_paths.strip(), 'guest generated persistent private host keys'
        result['persistent_host_keys_absent'] = True
        if phase == 'installer':
            installed = await execute_contract(score, stand, install=True)
            result['installation'] = installed
            assert installed['passed'], installed['reason']
            code, _ = await lease.ssh('wget -T 3 -qO- http://10.0.2.2:18081/', deadline=lease.deadline, check=False)
            assert code == 0
            verified = await execute_contract(score, stand)
            result['installer_verification'] = verified
            assert verified['passed'], verified['reason']
            result['receipt'] = score.environment_runtime_artifacts
            assert result['receipt'] is not None
        else:
            env = build_subprocess_env(cache_env=stand.cache_env)
            completed = await asyncio.to_thread(subprocess.run,['/bin/bash', '-c',
                'docker version --format "{{.Server.Os}}/{{.Server.Arch}}" && '
                'curl -fsS http://127.0.0.1:8080/message.txt'], env=env,
                capture_output=True, text=True, timeout=30)
            assert completed.returncode == 0, completed.stderr
            assert 'source-fixture-v1' in completed.stdout
            result['ordinary_shell'] = completed.stdout
            (lease.scratch/'scratch-marker').write_text('scratch-through-runtime\n')
            bind = await asyncio.to_thread(subprocess.run,['/bin/bash', '-c',
                'docker run --rm --network none -v "$COORDINARE_SCRATCH_DIR:/project:ro" '
                'busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e cat /project/scratch-marker'],
                env=env, capture_output=True, text=True, timeout=30)
            assert bind.returncode == 0 and bind.stdout == 'scratch-through-runtime\n', bind.stderr
            result['scratch_bind'] = bind.stdout
            outer = await asyncio.to_thread(subprocess.run,['/bin/sh', '-c', 'echo forbidden >/cache/forbidden'], capture_output=True, timeout=5)
            guest, _ = await lease.ssh('echo forbidden >/cache/forbidden', deadline=lease.deadline, check=False)
            assert outer.returncode != 0 and guest != 0
            result['cache_writes_denied'] = True
            result['egress_denied'] = {}
            for name, url in [('carrier', 'http://10.0.2.2:18081/'), ('metadata', 'http://169.254.169.254/'),
                              ('external', 'http://1.1.1.1/')]:
                code, _ = await lease.ssh('wget -T 3 -qO- '+url, deadline=lease.deadline, check=False)
                result['egress_denied'][name] = code != 0
            assert all(result['egress_denied'].values())
            binary_hash = hashlib.sha256((source/'binary.bin').read_bytes()).hexdigest()
            code, output = await lease.ssh('sha256sum '+str(source/'binary.bin'), deadline=lease.deadline)
            assert code == 0 and output.decode().split()[0] == binary_hash
            result['binary_source_verified'] = True
            result['receipt'] = score.environment_runtime_artifacts
    except BaseException as error:
        result['execution_error'] = {'type': type(error).__name__, 'message': str(error)[:4000]}
        raise
    finally:
        try:
            await close_environment(stand)
        except BaseException as error:
            result['cleanup_error'] = {'type': type(error).__name__, 'message': str(error)[:4000]}
            raise
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            if lease is not None and lease.cleanup is not None:
                result['cleanup'] = vars(lease.cleanup)
            Path('/evidence/'+phase+'.json').write_text(json.dumps(result, indent=2)+'\n')
    assert result.get('cleanup', {}).get('clean_shutdown') is True
    assert all(result['cleanup'][key] for key in ('qemu_reaped', 'keys_absent', 'listeners_absent', 'artifacts_unchanged', 'bounds_respected'))


if __name__ == '__main__':
    asyncio.run(run())
