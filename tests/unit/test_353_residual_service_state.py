"""Docker state left after the last command fails the records that should have removed it, and the run stays repairable.

Dettmore e2e-04: postgres-stop ran `docker rm -f dettmore-pg`, leaving the image's anonymous volume. The guest's seal
check refused to shut down with it present, so the whole run held as an incomplete runtime with no recorded cause,
and the execution repairs that would have fixed apt, the stop command and the rest never ran.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment import runtime_execution
from coordinare_service_inference.environment.models import CheckResult, VerificationReport
from coordinare_service_inference.environment.runtime_execution import (
    PrivateRuntimeIncompleteError,
    execute_with_runtime,
)

CLEAN = SimpleNamespace(confirmed=True, clean_shutdown=True, bounds_respected=True)
LISTING = b'containers=\nvolumes=0f3c9a\nnetworks=\n'


def _command(identifier, *argv):
    return SimpleNamespace(id=identifier, required=True, argv=list(argv or ['true']))


def _manifest(*, services=True):
    spec = SimpleNamespace(digest=lambda: 'b' * 64, policy=SimpleNamespace(resources=SimpleNamespace(max_seconds=60)))
    stop = _command('db-stop', 'docker', 'rm', '-f', 'db')
    return SimpleNamespace(runtime=spec, digest=lambda: 'a' * 64, install=[_command('install-tool')],
                           checks=[_command('check-tool'), _command('check-image', 'docker', 'run', 'img')],
                           services=[SimpleNamespace(id='db', stop=stop)] if services else [])


def _report(*, stop='pass'):
    results = [CheckResult(id='install-tool', required=True, status='pass'),
               CheckResult(id='check-tool', required=True, status='pass'),
               CheckResult(id='check-image', required=True, status='pass')]
    if stop is not None:
        results.append(CheckResult(id='db-stop', required=True, status=stop, output='removed'))
    return VerificationReport(manifest_digest='a' * 64, results=results)


async def _run(tmp_path, monkeypatch, *, report, manifest, listing=LISTING, cleanup=CLEAN, failing_list=False,
               shutdown_error=None, secret_names=(), redact_values=frozenset()):
    scripts: list[str] = []
    sealed: list[bool] = []
    lease = SimpleNamespace(activation_env={}, installation_passed=False, shutdown_error=None)

    async def ssh(script, *, deadline, check=True):
        scripts.append(script)
        if 'docker ps -a --format' in script:
            if failing_list:
                raise RuntimeError('guest command failed')
            return 0, listing
        return 0, b''

    async def close(**_kwargs):
        lease.shutdown_error = shutdown_error
        return cleanup

    async def opened(*_args, **_kwargs):
        return lease

    async def passthrough(_lease, operation):
        return await operation

    async def no_scan(_lease):
        return None

    async def seal(_lease, **_kwargs):
        sealed.append(True)

    async def finished(*_args, **_kwargs):
        return report

    lease.ssh, lease.close = ssh, close
    monkeypatch.setattr(runtime_execution, 'open_runtime', opened)
    monkeypatch.setattr(runtime_execution, 'run_guarded', passthrough)
    monkeypatch.setattr(runtime_execution, '_scan_bounds', no_scan)
    monkeypatch.setattr(runtime_execution, 'seal_runtime', seal)
    result = await execute_with_runtime(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path, install=True,
        scope='', secret_names=secret_names, execution_source_digest='c' * 64, runtime_artifacts=None,
        runtime_deadline=None, runtime_lease=None, commands=finished, redact_values=redact_values)
    return result, scripts, sealed


@pytest.mark.asyncio
async def test_a_volume_left_after_service_stop_fails_the_stop_and_is_removed_so_the_run_stays_repairable(
        tmp_path, monkeypatch):
    report, scripts, sealed = await _run(tmp_path, monkeypatch, report=_report(), manifest=_manifest())
    stop = next(result for result in report.results if result.id == 'db-stop')
    assert stop.status == 'fail'
    assert stop.output.startswith('removed\n') and '0f3c9a' in stop.output and 'docker rm -f -v' in stop.output
    assert {result.id for result in report.results if result.status == 'fail'} == {'db-stop'}
    assert not report.passed and sealed == []
    assert report.runtime_cleanup is True and report.runtime_incomplete is None
    assert any('docker volume rm' in script for script in scripts)


@pytest.mark.asyncio
async def test_without_a_service_the_docker_commands_that_ran_are_blamed(tmp_path, monkeypatch):
    report, _scripts, _sealed = await _run(tmp_path, monkeypatch, report=_report(stop=None), manifest=_manifest(services=False))
    assert {result.id for result in report.results if result.status == 'fail'} == {'check-image'}


@pytest.mark.asyncio
async def test_a_clean_guest_changes_nothing_and_still_seals(tmp_path, monkeypatch):
    clean = b'containers=\nvolumes=\nnetworks=\n'
    report, scripts, sealed = await _run(tmp_path, monkeypatch, report=_report(), manifest=_manifest(), listing=clean)
    assert report.passed and sealed == [True]
    assert not any('docker volume rm' in script for script in scripts)


@pytest.mark.asyncio
async def test_state_no_record_could_have_left_is_not_removed(tmp_path, monkeypatch):
    """Removing it would let unexplained state pass silently; the seal check still refuses it."""
    manifest = _manifest(services=False)
    manifest.checks = [_command('check-tool')]
    report = VerificationReport(manifest_digest='a' * 64, results=[CheckResult(id='check-tool', required=True, status='pass')])
    _report_out, scripts, _sealed = await _run(tmp_path, monkeypatch, report=report, manifest=manifest)
    assert not any('docker volume rm' in script for script in scripts)


@pytest.mark.asyncio
async def test_a_failed_listing_leaves_the_run_as_it_was(tmp_path, monkeypatch):
    report, scripts, sealed = await _run(tmp_path, monkeypatch, report=_report(), manifest=_manifest(), failing_list=True)
    assert report.passed and sealed == [True]
    assert not any('docker volume rm' in script for script in scripts)


@pytest.mark.asyncio
async def test_a_refused_shutdown_records_its_cause(tmp_path, monkeypatch):
    unclean = SimpleNamespace(confirmed=True, clean_shutdown=False, bounds_respected=True)
    with pytest.raises(PrivateRuntimeIncompleteError) as held:
        await _run(tmp_path, monkeypatch, report=_report(), manifest=_manifest(), cleanup=unclean,
                   listing=b'containers=\nvolumes=\nnetworks=\n', shutdown_error='RuntimeError: guest command failed')
    assert held.value.report.runtime_incomplete == ['clean_shutdown']
    assert 'guest command failed' in held.value.report.runtime_incomplete_detail


@pytest.mark.asyncio
async def test_secret_values_in_leftover_names_are_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv('OPERATOR_TOKEN', 'operator-value-123')
    listing = b'containers=db-operator-value-123\nvolumes=gen-secret-456\nnetworks=\n'
    report, _scripts, _sealed = await _run(tmp_path, monkeypatch, report=_report(), manifest=_manifest(), listing=listing,
                                           secret_names=('OPERATOR_TOKEN',), redact_values=frozenset({'gen-secret-456'}))
    stop = next(result for result in report.results if result.id == 'db-stop')
    assert stop.status == 'fail'
    assert 'operator-value-123' not in stop.output and 'gen-secret-456' not in stop.output
    assert '[REDACTED]' in stop.output


@pytest.mark.asyncio
async def test_the_live_lease_path_also_fails_the_stop_that_left_state(monkeypatch):
    lease = SimpleNamespace(spec=None, manifest_digest='a' * 64, source_digest='c' * 64, scratch='/tmp', deadline=1e12,
                            activation_env={}, install=True, installation_passed=False)
    removed: list[str] = []

    async def ssh(script, *, deadline, check=True):
        if 'docker ps -a --format' in script:
            return 0, LISTING
        removed.append(script)
        return 0, b''

    lease.ssh = ssh
    manifest = _manifest()
    lease.spec = manifest.runtime

    async def passthrough(_lease, operation):
        return await operation

    async def no_scan(_lease):
        return None

    async def finished(*_args, **_kwargs):
        return _report()

    monkeypatch.setattr(runtime_execution, 'run_guarded', passthrough)
    monkeypatch.setattr(runtime_execution, '_scan_bounds', no_scan)
    report = await execute_with_runtime(manifest, cache='/tmp', repo='/tmp', scratch='/tmp', install=True, scope='',
        secret_names=(), execution_source_digest='c' * 64, runtime_artifacts=None, runtime_deadline=None,
        runtime_lease=lease, commands=finished)
    assert not report.passed and lease.installation_passed is False
    assert any('docker volume rm' in script for script in removed)


@pytest.mark.asyncio
async def test_an_inherited_secret_in_a_leftover_name_is_redacted_like_command_output(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_SECRET', 'inherited-value-789')
    listing = b'containers=\nvolumes=db-inherited-value-789\nnetworks=\n'
    report, _scripts, _sealed = await _run(tmp_path, monkeypatch, report=_report(), manifest=_manifest(), listing=listing)
    stop = next(result for result in report.results if result.id == 'db-stop')
    assert stop.status == 'fail' and 'inherited-value-789' not in stop.output


def _listing(tmp_path, *, failing):
    """Runs the real listing script against a stub docker whose volume listing can fail."""
    import os
    import subprocess

    stub = tmp_path / 'docker'
    stub.write_text('#!/bin/sh\ncase "$1 $2" in\n  "ps -a") printf "db\\nweb\\n";;\n'
                    '  "volume ls") [ -n "$FAILVOL" ] && exit 1; printf "0f3c\\n";;\nesac\n')
    stub.chmod(0o755)
    env = {**os.environ, 'PATH': f'{tmp_path}:{os.environ["PATH"]}', **({'FAILVOL': '1'} if failing else {})}
    return subprocess.run(['sh', '-c', runtime_execution.RESIDUAL_LIST], capture_output=True, env=env, check=False)


def test_the_listing_names_everything_left(tmp_path):
    done = _listing(tmp_path, failing=False)
    assert done.returncode == 0
    assert done.stdout.decode().splitlines() == ['containers=db web', 'volumes=0f3c', 'networks=']


def test_a_failed_docker_listing_fails_the_whole_listing(tmp_path):
    """A partial listing must never read as empty: the reclaim would then blame and remove on half the facts."""
    assert _listing(tmp_path, failing=True).returncode != 0


def _creating_manifest():
    """Dettmore e2e-12's shape: an install step that creates a container, and a service that creates its own."""
    manifest = _manifest()
    manifest.install = [_command('install-host-ruby', 'bash', '-c',
                                 'set -e; docker create --name ruby-extract ruby:3 true; docker rm ruby-extract')]
    manifest.checks = [_command('check-pg', 'bash', '-c', 'docker exec devenv-pg pg_isready')]
    start = _command('db-start', 'bash', '-c', 'docker run -d --name devenv-pg -v pgdata:/var/lib/postgresql/data pg')
    manifest.services = [SimpleNamespace(id='db', start=start, health=None, init=None, stop=manifest.services[0].stop)]
    return manifest


def _creating_report():
    return VerificationReport(manifest_digest='a' * 64, results=[
        CheckResult(id='install-host-ruby', required=True, status='fail', output='tar: exiting'),
        CheckResult(id='check-pg', required=True, status='pass'),
        CheckResult(id='db-start', required=True, status='pass'),
        CheckResult(id='db-stop', required=True, status='pass', output='removed')])


def _by_id(report):
    return {result.id: result for result in report.results}


@pytest.mark.asyncio
async def test_a_container_an_install_step_created_blames_that_step_not_the_service_stops(tmp_path, monkeypatch):
    listing = b'containers=ruby-extract\nvolumes=\nnetworks=\n'
    report, scripts, _sealed = await _run(tmp_path, monkeypatch, report=_creating_report(),
                                          manifest=_creating_manifest(), listing=listing)
    results = _by_id(report)
    assert results['db-stop'].status == 'pass' and results['db-stop'].output == 'removed'
    install = results['install-host-ruby']
    assert install.status == 'fail' and install.output.startswith('tar: exiting\n')
    assert 'ruby-extract' in install.output and runtime_execution.COMMAND_RESIDUAL_GUIDANCE in install.output
    assert runtime_execution.RESIDUAL_GUIDANCE not in install.output
    assert any('docker volume rm' in script for script in scripts)


@pytest.mark.asyncio
async def test_state_a_service_created_blames_its_stop_and_never_a_check_that_only_names_it(tmp_path, monkeypatch):
    listing = b'containers=devenv-pg\nvolumes=pgdata\nnetworks=\n'
    report, _scripts, _sealed = await _run(tmp_path, monkeypatch, report=_creating_report(),
                                           manifest=_creating_manifest(), listing=listing)
    results = _by_id(report)
    assert results['check-pg'].status == 'pass'
    stop = results['db-stop']
    assert stop.status == 'fail' and 'devenv-pg' in stop.output and 'pgdata' in stop.output
    assert runtime_execution.RESIDUAL_GUIDANCE in stop.output
    assert results['install-host-ruby'].output == 'tar: exiting'


@pytest.mark.asyncio
async def test_unexplained_state_still_blames_the_stops_and_only_with_the_names_no_command_created(
        tmp_path, monkeypatch):
    listing = b'containers=ruby-extract\nvolumes=0f3c9a\nnetworks=\n'
    report, _scripts, _sealed = await _run(tmp_path, monkeypatch, report=_creating_report(),
                                           manifest=_creating_manifest(), listing=listing)
    results = _by_id(report)
    stop, install = results['db-stop'], results['install-host-ruby']
    assert stop.status == 'fail' and '0f3c9a' in stop.output and 'ruby-extract' not in stop.output
    assert 'ruby-extract' in install.output and '0f3c9a' not in install.output


@pytest.mark.asyncio
async def test_a_creator_that_did_not_run_is_not_blamed(tmp_path, monkeypatch):
    report = _creating_report()
    report.results = [result for result in report.results if result.id != 'install-host-ruby']
    listing = b'containers=ruby-extract\nvolumes=\nnetworks=\n'
    out, _scripts, _sealed = await _run(tmp_path, monkeypatch, report=report, manifest=_creating_manifest(),
                                        listing=listing)
    assert _by_id(out)['db-stop'].status == 'fail'


@pytest.mark.parametrize(('script', 'expected'), [
    ('docker create --name  ruby-extract img', {'containers': {'ruby-extract'}}),
    ('docker run -d \\\n  --name \\\n  web img', {'containers': {'web'}}),
    ('docker run --name=web img', {'containers': {'web'}}),
    ("docker create --name 'db'-old img", {'containers': {'db-old'}}),
    ('FOO=1 docker container run -d --name web img', {'containers': {'web'}}),
    ('docker volume create --driver local pgdata', {'volumes': {'pgdata'}}),
    ('docker network create --driver bridge appnet', {'networks': {'appnet'}}),
    ('docker run -v "pgdata":/data img', {'volumes': {'pgdata'}}),
    ('docker run --mount type=volume,source="pgdata",target=/d img', {'volumes': {'pgdata'}}),
    ('docker run --mount type=bind,source=/x,target=/d -v /h:/c -v ./w:/w img', {}),
    ('echo --name db; printf "docker volume create x"', {}),
    ('docker exec devenv-pg pg_isready; docker rm -f -v devenv-pg', {}),
    # Round 2 of review: docker stops reading run options at the image, and an option's value is never an option.
    ('docker run --rm alpine echo --name db', {}),
    ('docker run --rm -e --name=db alpine true', {}),
    ('docker run -it -p 80:80 --name web img', {'containers': {'web'}}),
    ('docker run -dp 80:80 --name web img', {'containers': {'web'}}),
    ('docker run -vpgdata:/d img', {'volumes': {'pgdata'}}),
    ('docker volume create pgdata --driver local', {'volumes': {'pgdata'}}),
    ('docker volume create --driver local', {}),
    ('docker network create appnet --driver bridge', {'networks': {'appnet'}}),
    ('docker create --name db\\\n-old alpine', {'containers': {'db-old'}}),
    # Round 3: -d is the driver for volume and network create.
    ('docker volume create -d local pgdata', {'volumes': {'pgdata'}}),
    ('docker network create -d bridge appnet', {'networks': {'appnet'}}),
    # A script the carrier lexer refuses names no creator: a miss only falls back, an invented creator would blame an
    # innocent command (round 3: a quoted or escaped separator, a heredoc body, a here-string).
    ("printf '%s ' ';' docker create --name fake alpine", {}),
    ('echo \\; docker create --name fake alpine', {}),
    ("cat <<'SCRIPT-EOF'\ndocker create --name fake alpine\nSCRIPT-EOF", {}),
    ('cat <<<EOF\ndocker create --name real alpine', {}),
    ('docker create --name ruby-extract alpine; printf "|"', {}),
    # Round 4: a command that might not run as written, or that docker would not run, names nothing; docker's own
    # reading decides the rest.
    ('if false; then docker create --name fake alpine; fi', {}),
    ('f() { docker create --name fake alpine; }; true', {}),
    ('docker # comment\ncreate --name fake alpine', {}),
    ('docker create --name ">log" alpine', {}),
    ('docker create --name db\\\\\n-old alpine', {}),
    ('docker create --help --name fake alpine', {}),
    ('docker create --name fake', {}),
    ('docker network create --name fake', {}),
    ('docker create --name first --name second alpine', {'containers': {'second'}}),
    ('docker run --mount type=volume,source=one,src=two,target=/d alpine', {'volumes': {'two'}}),
    # Dettmore e2e-12's install-host-ruby, as written.
    ('set -e; rm -rf /c/ruby-root/*; docker create --name ruby-extract ruby:3.4.2-slim true >/dev/null; '
     'docker export ruby-extract | tar -C /c/ruby-root -xf -; docker rm ruby-extract >/dev/null',
     {'containers': {'ruby-extract'}}),
])
def test_only_a_docker_invocation_that_creates_state_names_it(script, expected):
    created = runtime_execution.docker_created(['bash', '-c', script])
    assert {kind: names for kind, names in created.items() if names} == expected


def _service_manifest(*, init=None):
    manifest = _creating_manifest()
    manifest.install, manifest.checks = [], []
    manifest.services[0].init = init
    return manifest


@pytest.mark.asyncio
async def test_a_service_step_that_ran_blames_itself_when_its_stop_never_ran(tmp_path, monkeypatch):
    report = VerificationReport(manifest_digest='a' * 64, results=[
        CheckResult(id='db-start', required=True, status='pass')])
    listing = b'containers=devenv-pg\nvolumes=\nnetworks=\n'
    out, scripts, _sealed = await _run(tmp_path, monkeypatch, report=report, manifest=_service_manifest(),
                                       listing=listing)
    start = _by_id(out)['db-start']
    assert start.status == 'fail' and 'devenv-pg' in start.output
    assert any('docker volume rm' in script for script in scripts)


@pytest.mark.asyncio
async def test_a_service_step_that_never_ran_creates_nothing(tmp_path, monkeypatch):
    """init failed, so start (which names devenv-pg) never ran: the leftover is not start's, and not its stop's."""
    init = _command('db-init', 'bash', '-c', 'false')
    report = VerificationReport(manifest_digest='a' * 64, results=[
        CheckResult(id='db-init', required=True, status='fail', output='init failed'),
        CheckResult(id='db-stop', required=True, status='pass', output='removed')])
    listing = b'containers=devenv-pg\nvolumes=\nnetworks=\n'
    manifest = _service_manifest(init=init)
    blamed, unexplained = runtime_execution._residual_creators(
        manifest, {result.id: result for result in report.results}, {'containers': ['devenv-pg']})
    assert blamed == {} and unexplained == {'containers': ['devenv-pg']}
    out, _scripts, _sealed = await _run(tmp_path, monkeypatch, report=report, manifest=manifest, listing=listing)
    assert _by_id(out)['db-init'].output == 'init failed'


@pytest.mark.asyncio
async def test_a_record_is_blamed_once_for_created_and_unexplained_state(tmp_path, monkeypatch):
    manifest = _manifest(services=False)
    manifest.install = [_command('install-db', 'docker', 'create', '--name', 'db', 'img')]
    manifest.checks = []
    report = VerificationReport(manifest_digest='a' * 64, results=[
        CheckResult(id='install-db', required=True, status='pass')])
    listing = b'containers=db\nvolumes=0f3c9a\nnetworks=\n'
    out, _scripts, _sealed = await _run(tmp_path, monkeypatch, report=report, manifest=manifest, listing=listing)
    install = _by_id(out)['install-db']
    assert install.output.count('teardown found Docker state left behind') == 1
    assert 'containers db' in install.output and 'volumes 0f3c9a' in install.output


@pytest.mark.asyncio
async def test_a_mention_outside_docker_never_explains_state(tmp_path, monkeypatch):
    """echo --name db is not a creator, so with no other record the state stays unexplained and is not removed."""
    manifest = _manifest(services=False)
    manifest.install, manifest.checks = [_command('say', 'bash', '-c', 'echo --name db')], []
    report = VerificationReport(manifest_digest='a' * 64, results=[CheckResult(id='say', required=True, status='pass')])
    out, scripts, _sealed = await _run(tmp_path, monkeypatch, report=report, manifest=manifest,
                                       listing=b'containers=db\nvolumes=\nnetworks=\n')
    assert _by_id(out)['say'].status == 'pass'
    assert not any('docker volume rm' in script for script in scripts)


@pytest.mark.asyncio
async def test_state_only_partly_explained_is_blamed_but_not_removed(tmp_path, monkeypatch):
    """Two service starts ran and neither stop did: start A named its container, B's name was generated. A is blamed
    for its own; nothing answers for the other, so nothing is removed and the seal check still refuses it."""
    manifest = _manifest()
    start_a = _command('a-start', 'docker', 'run', '-d', '--name', 'known', 'img')
    start_b = _command('b-start', 'docker', 'run', '-d', 'img')
    manifest.install, manifest.checks = [], []
    manifest.services = [SimpleNamespace(id='a', init=None, start=start_a, health=None, stop=_command('a-stop')),
                         SimpleNamespace(id='b', init=None, start=start_b, health=None, stop=_command('b-stop'))]
    report = VerificationReport(manifest_digest='a' * 64, results=[
        CheckResult(id='a-start', required=True, status='pass'), CheckResult(id='b-start', required=True, status='pass')])
    out, scripts, _sealed = await _run(tmp_path, monkeypatch, report=report, manifest=manifest,
                                       listing=b'containers=known random-other\nvolumes=\nnetworks=\n')
    results = _by_id(out)
    assert results['a-start'].status == 'fail' and 'known' in results['a-start'].output
    assert 'random-other' not in results['a-start'].output and results['b-start'].status == 'pass'
    assert not any('docker volume rm' in script for script in scripts)


def test_a_continuation_inside_single_quotes_stays_in_the_word():
    """Bash keeps a backslash-newline inside single quotes, so the name is never joined into db-old."""
    created = runtime_execution.docker_created(['bash', '-c', "docker create --name 'db\\\n-old' alpine"])
    assert created['containers'] == {'db\\\n-old'}
