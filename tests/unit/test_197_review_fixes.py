from __future__ import annotations

import asyncio
import io
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import yaml

from coordinare import docker_setup
from coordinare.config import ProjectConfiguration
from coordinare.init_files import SetupError
from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.presets import expand_preset
from coordinare.services import performer_lifecycle
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.transport.http_transport import PerformerHTTPClient
from coordinare.user_config import read_environment
from tests.unit.test_195_init import answers_file, invoke
from tests.unit.test_197_docker_setup import PINNED, RELEASE
from tests.unit.test_197_docker_setup import docker as docker_fixture
from tests.unit.test_197_docker_setup import setup as setup_fixture
from tests.utils.smoke_release import release_script_env

docker = docker_fixture
setup = setup_fixture

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('harness', ['codex', 'claude_code', 'opencode'])
def test_197_existing_containers_expands_exactly_as_origin_main(harness: str) -> None:
    snapshot = json.loads((ROOT / 'tests/fixtures/197_containers_origin_main.json').read_text())
    assert expand_preset({'preset': 'containers', 'harness': harness, 'github_repo': 'demo'}) == snapshot[harness]
    explicit = {'preset': 'containers', 'harness': harness, 'github_repo': 'demo', 'performer_image': 'local:custom'}
    expected = {**snapshot[harness], 'performer_image': 'local:custom'}
    expected['performer_endpoints'][0]['image'] = 'local:custom'
    assert expand_preset(explicit) == expected


@pytest.mark.parametrize('tag,code', [('2026.10.999', 0), ('', 1), ('null', 1)])
def test_197_workflow_resolves_release_at_job_time(tmp_path: Path, tag: str, code: int) -> None:
    workflow = yaml.load((ROOT / '.github/workflows/docker_setup.yml').read_text(), Loader=yaml.BaseLoader)
    steps = workflow['jobs']['docker-setup']['steps']
    step = next(s for s in steps if s.get('id') == 'release')
    assert step['env']['GH_TOKEN'] == '${{ github.token }}'
    assert step['run'] == 'bash scripts/resolve_smoke_release.sh'
    output = tmp_path / 'output'
    result = subprocess.run(['bash', str(ROOT / 'scripts/resolve_smoke_release.sh')], capture_output=True, text=True,
                            env=release_script_env(tmp_path, tag), check=False)
    assert result.returncode == code
    if code:
        assert 'published release' in result.stderr.lower()
    else:
        assert output.read_text().strip() == 'tag=' + tag
    assert workflow['jobs']['docker-setup']['strategy']['matrix']['variant'] == ['base', 'full']
    paths = workflow['on']['pull_request']['paths']
    assert {'src/coordinare/doctor.py', 'src/coordinare/launch_preflight.py',
            'src/coordinare/services/performer_lifecycle.py', 'agent/performer/Dockerfile.base',
            'agent/performer/entrypoint.sh'} <= set(paths)


@pytest.mark.parametrize('error', ['proxy not found', 'TLS certificate not found', 'x509: certificate signed by unknown authority'])
def test_197_pull_transport_errors_are_not_missing_manifests(docker, tmp_path: Path, error: str) -> None:
    docker.fail_operation, docker.error = 'pull', error
    with pytest.raises(SetupError, match=r'network|TLS|proxy') as caught:
        docker_setup.prepare_image('full', RELEASE, tmp_path, runner=docker)
    assert 'No published image' not in str(caught.value)


def test_197_exec_permission_is_not_socket_permission(docker, tmp_path: Path) -> None:
    docker.exception = PermissionError()
    with pytest.raises(SetupError, match='executable permissions') as caught:
        docker_setup.prepare_image('full', RELEASE, tmp_path, runner=docker)
    assert 'socket' not in str(caught.value)


@pytest.mark.parametrize('failure', ['run_timeout', 'health_timeout', 'cleanup_timeout'])
def test_197_smoke_real_entrypoint_and_cleanup_on_failures(monkeypatch, failure: str) -> None:
    commands = []
    def runner(command, timeout):
        commands.append(command)
        assert 0 < timeout <= 10
        if command[1] == 'run' and failure == 'run_timeout':
            raise subprocess.TimeoutExpired(command, timeout)
        if command[1] == 'exec':
            if failure == 'health_timeout':
                raise subprocess.TimeoutExpired(command, timeout)
            return subprocess.CompletedProcess(command, 0, 'ready', '')
        if command[1] == 'rm' and failure == 'cleanup_timeout':
            raise subprocess.TimeoutExpired(command, timeout)
        return subprocess.CompletedProcess(command, 0, 'container-id', '')
    endpoint = PerformerEndpointConfig(id='starter', mode='ephemeral', roles=['implementer'], image=PINNED, env={'BACKEND': 'codex'})
    monkeypatch.setattr(docker_setup.time, 'monotonic', MagicMock(side_effect=[0, 0, 0, 301, 301]))
    with pytest.raises(SetupError, match=r'timed out|cleanup'):
        docker_setup.smoke_container(endpoint, runner=runner)
    run = commands[0]
    assert '--entrypoint' not in run and run[-1] == PINNED
    assert '--name' in run and '--label' in run
    name = run[run.index('--name') + 1]
    assert commands[-1] == ['docker', 'rm', '-f', name]


@pytest.mark.parametrize('availability,backends,ready', [('idle', ['codex'], True), ('starting', ['codex'], False), ('idle', [], False)])
def test_197_smoke_requires_harness_readiness(monkeypatch, availability: str, backends: list[str], ready: bool) -> None:
    import urllib.request
    monkeypatch.setenv('BACKEND', 'codex')
    responses = [io.BytesIO(b'{}'), io.BytesIO(json.dumps({'availability': availability, 'capabilities': {'backends': backends}}).encode())]
    monkeypatch.setattr(urllib.request, 'urlopen', MagicMock(side_effect=responses))
    if ready:
        exec(docker_setup.SMOKE_PROBE, {})
    else:
        with pytest.raises((AssertionError, SystemExit, RuntimeError)):
            exec(docker_setup.SMOKE_PROBE, {})


def test_197_init_runs_probe_and_offers_skip(setup, docker, monkeypatch, tmp_path: Path, capsys) -> None:
    answers = answers_file(tmp_path, run_mode='docker')
    assert invoke(monkeypatch, '--answers', str(answers)) == 0
    assert any(c[1] == 'run' for c in docker.calls)
    assert any(c[1] == 'exec' for c in docker.calls)
    assert any(c[1] == 'rm' for c in docker.calls)
    assert 'init --force --run-mode docker' in capsys.readouterr().out
    docker.calls.clear()
    assert invoke(monkeypatch, '--answers', str(answers), '--force', '--skip-smoke') == 0
    assert not any(c[1] == 'run' for c in docker.calls)
    assert 'skipped' in capsys.readouterr().out.lower()


def test_197_probe_failure_does_not_complete_setup(setup, docker, monkeypatch, tmp_path: Path) -> None:
    docker.fail_operation = 'run'
    assert invoke(monkeypatch, '--answers', str(answers_file(tmp_path, run_mode='docker'))) == 1
    assert not setup.exists()


def test_197_auth_wired_from_private_file_to_container_and_daemon(setup, docker, monkeypatch, tmp_path: Path) -> None:
    assert invoke(monkeypatch, '--answers', str(answers_file(tmp_path, run_mode='docker'))) == 0
    raw = yaml.safe_load(setup.read_text())
    secrets = read_environment(setup.parent / 'coordinare.env')
    token_ref = raw['performer_endpoints'][0]['auth_token']
    token = secrets[token_ref[2:-1]]
    assert len(token) >= 32
    assert token not in setup.read_text()
    config = ProjectConfiguration.from_yaml(setup)
    endpoint = config.performer_endpoints[0]
    assert endpoint.auth_token.get_secret_value() == token
    paths = []
    async def runner(*args, **kwargs):
        assert all(token not in arg for arg in args)
        if args[0] == 'run':
            path = Path(args[args.index('--env-file') + 1])
            paths.append(path)
            assert path.stat().st_mode & 0o777 == 0o600
            assert 'PERFORMER_AUTH_TOKEN=' + token in path.read_text()
            assert '127.0.0.1:0:8088' in args
            return 0, 'container', ''
        return 0, '127.0.0.1:12345', ''
    monkeypatch.setattr(performer_lifecycle, '_run_docker', runner)
    started = asyncio.run(performer_lifecycle.start_ephemeral(endpoint))
    assert all(not path.exists() for path in paths)
    service = HTTPPerformerService(endpoint)
    assert service._auth_token() == token
    async def check_request():
        from performer.server import create_app_from_env
        from performer.server.models import PerformerCapabilities

        # Use the actual server auth middleware with the token delivered to Docker.
        monkeypatch.setenv('PERFORMER_AUTH_TOKEN', token)
        monkeypatch.setattr('performer.server.probe_capabilities', lambda: PerformerCapabilities(backends=['codex'], tool_flags=[]))
        app = create_app_from_env()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
            unauthorized = await http.get(started.endpoint + '/status')
            assert unauthorized.status_code == 401
            wrong = await http.get(started.endpoint + '/status', headers={'Authorization': 'Bearer wrong'})
            assert wrong.status_code == 401
            client = PerformerHTTPClient(started.endpoint, auth_token=service._auth_token(), client=http)
            status = await client.get_status()
            assert status.availability == 'idle' and status.auth_enabled
    asyncio.run(check_request())
    assert invoke(monkeypatch, '--answers', str(answers_file(tmp_path, run_mode='docker')), '--force') == 0
    assert read_environment(setup.parent / 'coordinare.env')[token_ref[2:-1]] == token


@pytest.mark.parametrize('bind,host', [('127.0.0.1', '127.0.0.1'), ('0.0.0.0', 'host.docker.internal')])
def test_197_publish_address_is_configurable(monkeypatch, bind: str, host: str) -> None:
    endpoint = PerformerEndpointConfig(id='starter', mode='ephemeral', image=PINNED, roles=['implementer'], bind_host=bind, connect_host=host, auth_token='test-token')
    boundary = AsyncMock(side_effect=[(0, 'container', ''), (0, bind + ':12345', '')])
    monkeypatch.setattr(performer_lifecycle, '_run_docker', boundary)
    started = asyncio.run(performer_lifecycle.start_ephemeral(endpoint))
    assert bind + ':0:8088' in boundary.call_args_list[0].args
    assert started.endpoint == 'http://' + host + ':12345'


def test_197_init_probe_credentials_use_private_env_file(setup, docker, monkeypatch, tmp_path: Path, capsys) -> None:
    observed = {}
    paths = []
    def runner(command, timeout):
        if command[1] == 'run':
            path = Path(command[command.index('--env-file') + 1])
            paths.append(path)
            assert path.stat().st_mode & 0o777 == 0o600
            observed.update(read_environment(path))
        return docker(command, timeout)
    monkeypatch.setattr(docker_setup, 'run_docker', runner)
    assert invoke(monkeypatch, '--answers', str(answers_file(tmp_path, run_mode='docker'))) == 0
    assert observed['COORDINARE_SMOKE_GITHUB_TOKEN'] == 'private-token'
    assert observed['COORDINARE_SMOKE_MODEL_TOKEN'] == 'private-api-key'
    assert observed['COORDINARE_SMOKE_MODEL_URL'] == 'https://api.openai.com/v1/models'
    assert len(observed['PERFORMER_AUTH_TOKEN']) >= 32
    output = capsys.readouterr()
    for token in (observed['PERFORMER_AUTH_TOKEN'], 'private-token', 'private-api-key'):
        assert token not in output.out + output.err + setup.read_text()
        assert all(token not in argument for command in docker.calls for argument in command)
    assert all(not path.exists() for path in paths)


@pytest.mark.parametrize('kind', ['openai', 'anthropic', 'litellm'])
def test_197_container_connectivity_checks_reach_github_and_model(monkeypatch, kind: str) -> None:
    import urllib.request
    monkeypatch.setenv('BACKEND', 'codex')
    monkeypatch.setenv('PERFORMER_AUTH_TOKEN', 'performer-secret')
    monkeypatch.setenv('COORDINARE_SMOKE_GITHUB_TOKEN', 'github-secret')
    monkeypatch.setenv('COORDINARE_SMOKE_MODEL_URL', 'https://model.example/v1/models')
    monkeypatch.setenv('COORDINARE_SMOKE_MODEL_TOKEN', 'model-secret')
    monkeypatch.setenv('COORDINARE_SMOKE_MODEL_KIND', kind)
    responses = [io.BytesIO(b'{}'), io.BytesIO(b'{"availability":"idle","auth_enabled":true,"capabilities":{"backends":["codex"]}}'), io.BytesIO(b'{}'), io.BytesIO(b'{}')]
    boundary = MagicMock(side_effect=responses)
    monkeypatch.setattr(urllib.request, 'urlopen', boundary)
    exec(docker_setup.SMOKE_PROBE, {})
    requests = [call.args[0] for call in boundary.call_args_list]
    assert [request.full_url for request in requests] == ['http://127.0.0.1:8088/healthz', 'http://127.0.0.1:8088/status', 'https://api.github.com/user', 'https://model.example/v1/models']
    assert requests[1].get_header('Authorization') == 'Bearer performer-secret'
    assert requests[2].get_header('Authorization') == 'Bearer github-secret'
    if kind == 'anthropic':
        assert requests[3].get_header('X-api-key') == 'model-secret'
    else:
        assert requests[3].get_header('Authorization') == 'Bearer model-secret'


@pytest.mark.parametrize('failure', ['error', 'timeout'])
def test_197_ephemeral_auth_file_removed_when_start_fails(monkeypatch, failure: str) -> None:
    paths = []
    async def runner(*args, **kwargs):
        path = Path(args[args.index('--env-file') + 1])
        paths.append(path)
        assert 'secret-value' not in str(args)
        if failure == 'timeout':
            raise performer_lifecycle.ContainerStartError('timed out')
        return 1, '', 'failed'
    monkeypatch.setattr(performer_lifecycle, '_run_docker', runner)
    config = PerformerEndpointConfig(id='starter', mode='ephemeral', roles=['implementer'], image=PINNED, auth_token='secret-value')
    with pytest.raises(performer_lifecycle.ContainerStartError):
        asyncio.run(performer_lifecycle.start_ephemeral(config))
    assert paths and all(not path.exists() for path in paths)


def test_197_each_endpoint_gets_a_distinct_token_and_explicit_settings(monkeypatch, tmp_path: Path) -> None:
    from coordinare.init_files import configure_containers
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path))
    raw = {'preset': 'containers', 'harness': 'codex', 'github_repo': 'demo', 'performer_endpoints': [
        {'id': 'starter', 'mode': 'ephemeral', 'roles': ['implementer'], 'image': 'old'},
        {'id': 'other', 'mode': 'ephemeral', 'roles': ['reviewer'], 'image': 'custom:full', 'bind_host': '0.0.0.0', 'connect_host': 'host.docker.internal'},
    ]}
    secrets = {}
    configure_containers(raw, PINNED, secrets)
    assert len(secrets) == 2 and len(set(secrets.values())) == 2
    assert raw['performer_endpoints'][0]['env']['TMPDIR'] == '/workspace'
    assert raw['performer_endpoints'][1]['bind_host'] == '0.0.0.0'
    assert raw['performer_endpoints'][1]['connect_host'] == 'host.docker.internal'
    assert raw['performer_endpoints'][1]['image'] == 'custom:full'
    previous = secrets.copy()
    configure_containers(raw, PINNED, secrets)
    assert previous == secrets


def test_197_container_connectivity_requires_github_credential() -> None:
    config = ProjectConfiguration.model_construct(github_token=None)
    with pytest.raises(SetupError, match='GitHub token'):
        docker_setup.smoke_environment(config)


def test_197_smoke_refuses_disabled_auth(monkeypatch) -> None:
    import urllib.request
    monkeypatch.setenv('BACKEND', 'codex')
    monkeypatch.setenv('PERFORMER_AUTH_TOKEN', 'expected-token')
    responses = [io.BytesIO(b'{}'), io.BytesIO(b'{"availability":"idle","auth_enabled":false,"capabilities":{"backends":["codex"]}}')]
    monkeypatch.setattr(urllib.request, 'urlopen', MagicMock(side_effect=responses))
    with pytest.raises((AssertionError, RuntimeError), match='authentication'):
        exec(docker_setup.SMOKE_PROBE, {})
