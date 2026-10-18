from __future__ import annotations

import os
import sys

import pytest
from coordinare_service_inference.environment.models import EnvironmentManifest
from coordinare_service_inference.environment.render import render_activate, render_verify
from coordinare_service_inference.environment.runner import run_manifest


def contract(checks, **changes):
    data = {
        "schema_version": 1,
        "symphony_name": "fixture",
        "source_commit": "a" * 40,
        "inputs": {},
        "requirements": [],
        "install": [],
        "checks": checks,
        "activation": {},
        "services": [],
        "empty_reason": "uses base image",
        "policy_version": "351-v1",
    }
    data.update(changes)
    return EnvironmentManifest.model_validate(data)


@pytest.mark.asyncio
async def test_inherited_shell_startup_hooks_remain_unset(tmp_path, monkeypatch):
    hook = tmp_path / "startup-hook"
    marker = tmp_path / "hook-ran"
    hook.write_text(f"touch '{marker}'\n")
    monkeypatch.setenv("BASH_ENV", str(hook))
    monkeypatch.setenv("ENV", str(hook))
    manifest = contract([{
        "id": "shell",
        "argv": ["bash", "-c", 'test -z "${BASH_ENV+x}" && test -z "${ENV+x}"'],
    }])
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path)
    assert report.passed
    assert report.results[0].exit_code == 0
    assert not marker.exists()


@pytest.mark.asyncio
async def test_required_false_cannot_forge_success(tmp_path):
    manifest = contract(
        [
            {
                "id": "lie",
                "argv": [sys.executable, "-c", 'print("{\\"passed\\":true}");raise SystemExit(1)'],
            },
            {"id": "optional", "argv": ["true"], "required": False},
        ],
    )
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path)
    assert not report.passed
    assert report.results[0].exit_code == 1


@pytest.mark.asyncio
async def test_prerequisite_timeout_redaction_and_stdin(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_TOKEN", "private-secret-value")
    manifest = contract(
        [
            {
                "id": "timeout",
                "argv": [sys.executable, "-c", "import time;time.sleep(5)"],
                "timeout_seconds": 0.05,
            },
            {"id": "dependent", "argv": ["true"], "depends_on": ["timeout"]},
            {
                "id": "stdin",
                "argv": [
                    sys.executable,
                    "-c",
                    'import sys;assert sys.stdin.read()=="";print("private-secret-value"+"x"*5000)',
                ],
            },
        ],
    )
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path)
    assert [r.status for r in report.results] == ["indeterminate", "not_run", "pass"]
    assert "private-secret-value" not in report.results[-1].output
    assert len(report.results[-1].output.encode()) <= 4096


@pytest.mark.asyncio
async def test_activation_root_expansion_and_optional_failure(tmp_path):
    manifest = contract(
        [
            {
                "id": "env",
                "argv": [sys.executable, "-c", 'import os;assert os.environ["ROOT"]==os.getcwd()'],
            },
            {"id": "optional", "argv": ["false"], "required": False},
        ],
        activation={"env": {"ROOT": "{repo}"}},
    )
    assert (await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path)).passed


def test_render_uses_trusted_runner_and_declarative_activation(tmp_path):
    manifest = contract(
        [{"id": "base", "argv": ["true"]}],
        activation={"path": ["{cache}/bin"], "env": {"ROOT": "{cache}"}},
    )
    script = render_verify(manifest, cache_mount_path=str(tmp_path))
    assert "coordinare_service_inference.environment.runner" in script
    assert "source " not in script
    assert "ROOT" in render_activate(manifest, cache_mount_path=str(tmp_path))


@pytest.mark.asyncio
async def test_service_init_background_health_and_teardown(tmp_path):
    pidfile = tmp_path / "pid"
    marker = tmp_path / "stop"
    service = {
        "id": "arbitrary-service",
        "init": {"id": "init", "argv": [sys.executable, "-c", 'print("init")']},
        "start": {
            "id": "start",
            "argv": [
                sys.executable,
                "-c",
                'import os,time,pathlib;pathlib.Path("pid").write_text(str(os.getpid()));time.sleep(30)',
            ],
        },
        "health": {
            "id": "health",
            "argv": [sys.executable, "-c", 'from pathlib import Path;assert Path("pid").exists()'],
        },
        "stop": {
            "id": "stop",
            "argv": [sys.executable, "-c", 'from pathlib import Path;Path("stop").touch()'],
        },
        "required": True,
    }
    manifest = contract([{"id": "usable", "argv": ["true"]}], services=[service])
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path)
    assert report.passed is (sys.platform == "linux")
    assert marker.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


@pytest.mark.asyncio
async def test_nonlinux_live_service_start_is_indeterminate_and_still_cleaned_up(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    manifest = contract([{"id": "usable", "argv": ["true"]}], services=[{
        "id": "foreground",
        "start": {"id": "start", "argv": [sys.executable, "-c",
            'import os,pathlib,time;pathlib.Path("pid").write_text(str(os.getpid()));time.sleep(30)']},
        "health": {"id": "health", "argv": ["true"]},
        "stop": {"id": "stop", "argv": [sys.executable, "-c",
            'from pathlib import Path;Path("stopped").touch()']},
    }])
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path)
    results = {result.id: result for result in report.results}
    assert results["start"].required
    assert results["start"].status == "indeterminate"
    assert results["start"].exit_code is None
    assert "Linux" in results["start"].output
    assert results["health"].status == "not_run"
    assert results["usable"].status == "not_run"
    assert not report.passed
    assert results["stop"].status == "pass"
    assert (tmp_path / "stopped").exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / "pid").read_text()), 0)


@pytest.mark.asyncio
async def test_nonlinux_completed_service_start_can_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    manifest = contract([{"id": "usable", "argv": ["true"]}], services=[{
        "id": "completed",
        "start": {"id": "start", "argv": ["true"]},
        "health": {"id": "health", "argv": ["true"]},
        "stop": {"id": "stop", "argv": ["true"]},
    }])
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path)
    start = next(result for result in report.results if result.id == "start")
    assert start.status == "pass"
    assert start.exit_code == 0
    assert report.passed


@pytest.mark.asyncio
async def test_failed_service_health_blocks_checks_but_still_stops(tmp_path):
    manifest = contract(
        [{"id": "usable", "argv": ["true"]}],
        services=[
            {
                "id": "service",
                "start": {"id": "start", "argv": ["true"]},
                "health": {"id": "health", "argv": ["false"]},
                "stop": {"id": "stop", "argv": ["true"]},
            },
        ],
    )
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path)
    results = {result.id: result for result in report.results}
    assert not report.passed
    assert results["usable"].status == "not_run"
    assert results["stop"].status == "pass"


@pytest.mark.asyncio
async def test_cancellation_kills_command_group(tmp_path):
    import asyncio

    pidfile = tmp_path / "pid"
    manifest = contract(
        [
            {
                "id": "long",
                "argv": [
                    sys.executable,
                    "-c",
                    'import os,time,pathlib;pathlib.Path("pid").write_text(str(os.getpid()));time.sleep(30)',
                ],
            },
        ],
    )
    task = asyncio.create_task(
        run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path),
    )
    for _ in range(100):
        if pidfile.exists():
            break
        await asyncio.sleep(0.01)
    assert pidfile.exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


@pytest.mark.asyncio
async def test_render_activation_repeat_and_literal_shell_characters(tmp_path):
    import asyncio

    manifest = contract(
        [{"id": "usable", "argv": ["true"]}],
        activation={"path": ["{cache}/bin"], "env": {"VALUE": 'literal $(touch BAD); "quoted"'}},
    )
    activation = render_activate(manifest, cache_mount_path=str(tmp_path))
    process = await asyncio.create_subprocess_exec(
        "bash",
        "--noprofile",
        "--norc",
        "-c",
        activation + "\n" + activation + "\n" + 'printf "%s\\n%s" "$VALUE" "$PATH"',
        cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    assert process.returncode == 0
    value, path = stdout.decode().split("\n", 1)
    assert value == 'literal $(touch BAD); "quoted"'
    assert path.split(os.pathsep).count(str(tmp_path / "bin")) == 1
    assert not (tmp_path / "BAD").exists()


@pytest.mark.asyncio
async def test_rendered_verifier_stdout_is_one_trusted_json_record(tmp_path):
    import asyncio
    import json
    import shlex

    manifest = contract([{'id': 'forge', 'argv': [sys.executable, '-c',
        'print("{\\"passed\\":true}");raise SystemExit(1)']}])
    script = render_verify(manifest, cache_mount_path=str(tmp_path))
    # This host is macOS; production images use the fixed /usr/local/bin/python.
    script = script.replace('/usr/local/bin/python', shlex.quote(sys.executable))
    process = await asyncio.create_subprocess_exec(
        'bash', '--noprofile', '--norc', '-c', script,
        cwd=tmp_path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 1, stderr.decode()
    report = json.loads(stdout)
    assert report['passed'] is False
    assert report['results'][0]['exit_code'] == 1


@pytest.mark.asyncio
async def test_tail_redacts_secret_and_missing_binary_is_failure(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_SECRET', 'sensitive-value-987')
    manifest = contract([
        {'id': 'redact', 'argv': [sys.executable, '-c',
            'print("x"*5000+"sensitive-value-987")']},
        {'id': 'missing', 'argv': ['/unavailable/arbitrary-tool']},
    ])
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path)
    assert 'sensitive-value-987' not in report.results[0].output
    assert '[REDACTED]' in report.results[0].output
    assert report.results[0].output_truncated
    assert report.results[1].status == 'fail'
    assert not report.passed


@pytest.mark.asyncio
async def test_detached_descendant_cannot_hold_output_drain_unbounded(tmp_path):
    import time

    # Keep the parent alive beyond its deadline: Linux can otherwise legitimately
    # finish a successful parent and reap its detached child before the timeout.
    manifest = contract([{'id': 'detached', 'timeout_seconds': 0.05,
        'argv': [sys.executable, '-c',
            'import subprocess,sys,time;'
            'subprocess.Popen([sys.executable,"-c","import time;time.sleep(2)"],start_new_session=True);'
            'time.sleep(20)']}])
    started = time.monotonic()
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path)
    assert time.monotonic() - started < 1.5
    assert not report.passed


@pytest.mark.asyncio
async def test_activation_moves_declared_path_ahead_of_existing_image_tools(tmp_path):
    import asyncio

    manifest = contract([{'id': 'base', 'argv': ['true']}],
                        activation={'path': ['{cache}/bin', '{cache}/other']})
    activation = render_activate(manifest, cache_mount_path=str(tmp_path))
    process = await asyncio.create_subprocess_exec(
        'bash', '--noprofile', '--norc', '-c',
        activation + '\n' + activation + '\nprintf "%s" "$PATH"',
        env={**os.environ, 'PATH': f'/usr/bin:{tmp_path}/other:/bin:{tmp_path}/bin'},
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    assert stdout.decode().split(':') == [str(tmp_path / 'bin'), str(tmp_path / 'other'), '/usr/bin', '/bin']


@pytest.mark.asyncio
async def test_clean_verification_executes_required_checks_in_every_scope(tmp_path):
    manifest = contract([
        {"id": "baseline", "argv": ["true"]},
        {"id": "scoped", "argv": ["false"], "scope": ["implementer"]},
    ])
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path)
    scoped = next(result for result in report.results if result.id == "scoped")
    assert scoped.required and scoped.status == "fail"
    assert not report.passed


@pytest.mark.asyncio
async def test_explicit_secret_names_and_credential_urls_are_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("FIXTURE_DSN", "unusual-private-value")
    monkeypatch.setenv("DATABASE_URL", "postgres://private_user:private_password@database/db")
    manifest = contract([{"id": "baseline", "argv": [sys.executable, "-c",
        "import os;print(os.environ['FIXTURE_DSN']);print(os.environ['DATABASE_URL'])"]}])
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path,
                                secret_names=["FIXTURE_DSN"])
    serialized = report.model_dump_json()
    assert "unusual-private-value" not in serialized
    assert "private_password" not in serialized
    assert report.passed


@pytest.mark.asyncio
@pytest.mark.parametrize('overlay', ['activation', 'command', 'service'])
async def test_explicit_secret_values_survive_declaration_overlays(tmp_path, monkeypatch, overlay):
    monkeypatch.setenv('FIXTURE_DSN', 'private-through-alias')
    monkeypatch.setenv('ALIAS', 'private-through-alias')
    env = {'FIXTURE_DSN': 'public'}
    printer = {'id': 'baseline', 'argv': [sys.executable, '-c', 'import os;print(os.environ["ALIAS"])']}
    changes = {}
    if overlay == 'activation':
        changes['activation'] = {'env': env}
    elif overlay == 'command':
        printer['env'] = env
    else:
        changes['services'] = [{'id': 'svc',
            'start': {'id': 'start', 'argv': ['true']},
            'health': {'id': 'health', 'argv': printer['argv'], 'env': env},
            'stop': {'id': 'stop', 'argv': ['true']}}]
        printer['argv'] = ['true']
    manifest = contract([printer], **changes)
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path,
                                secret_names=['FIXTURE_DSN'])
    assert 'private-through-alias' not in report.model_dump_json()
    assert report.passed


@pytest.mark.parametrize('phase', ['activation', 'command', 'service'])
@pytest.mark.parametrize('channel', ['COORDINARE_ENV_SECRET_NAMES', 'COORDINARE_ENV_SECRET_VALUES'])
def test_secret_name_channel_cannot_be_declared_by_manifest(phase, channel):
    from pydantic import ValidationError

    env = {channel: '${' + channel + '}'}
    printer = {'id': 'baseline', 'argv': ['true']}
    changes = {}
    if phase == 'activation':
        changes['activation'] = {'env': env}
    elif phase == 'command':
        printer['env'] = env
    else:
        changes['services'] = [{'id': 'svc',
            'start': {'id': 'start', 'argv': ['true'], 'env': env},
            'health': {'id': 'health', 'argv': ['true']},
            'stop': {'id': 'stop', 'argv': ['true']}}]
    with pytest.raises(ValidationError):
        contract([printer], **changes)


@pytest.mark.asyncio
async def test_cli_restores_transient_secret_values_after_trusted_python_start(tmp_path):
    import asyncio
    import base64
    import json

    manifest = contract([{'id': 'baseline', 'argv': [sys.executable, '-c',
        'import os;assert os.environ["UNUSUAL_DSN"]=="restored-secret";print(os.environ["UNUSUAL_DSN"])']}])
    env = {**os.environ, 'COORDINARE_ENV_SECRET_VALUES': base64.b64encode(
        json.dumps({'UNUSUAL_DSN': 'restored-secret'}).encode()).decode()}
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-I', '-m', 'coordinare_service_inference.environment.runner',
        '--manifest', manifest.canonical_json(), '--cache', str(tmp_path), '--repo', str(tmp_path),
        '--scratch', str(tmp_path), env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    assert 'restored-secret' not in stdout.decode()
    assert json.loads(stdout)['passed']


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux command-local subreaper adapter')
@pytest.mark.asyncio
@pytest.mark.parametrize('timeout', [False, True])
@pytest.mark.parametrize('closed_streams', [False, True])
async def test_linux_supervisor_reaps_setsid_descendants(tmp_path, timeout, closed_streams):
    import time

    code = (
        'import subprocess,sys,pathlib,time;'
        'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"],'
        'start_new_session=True'
        + (',stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL' if closed_streams else '')
        + ');'
        'pathlib.Path("escaped").write_text(str(p.pid));'
        + ('time.sleep(30)' if timeout else 'time.sleep(0)')
    )
    manifest = contract([{'id': 'baseline', 'argv': [sys.executable, '-c', code],
                          'timeout_seconds': 0.5 if timeout else 3.0}])
    started = time.monotonic()
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path)
    assert time.monotonic() - started < 1.5
    assert report.passed is not timeout
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / 'escaped').read_text()), 0)


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux command-local subreaper adapter')
@pytest.mark.asyncio
async def test_linux_supervisor_preserves_daemonized_service_until_teardown(tmp_path):
    code = (
        'import subprocess,sys,pathlib;'
        'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"],'
        'start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);'
        'pathlib.Path("service_pid").write_text(str(p.pid))'
    )
    health = 'import os,pathlib;os.kill(int(pathlib.Path("service_pid").read_text()),0)'
    manifest = contract([{'id': 'baseline', 'argv': [sys.executable, '-c', health]}], services=[{
        'id': 'daemon', 'start': {'id': 'start', 'argv': [sys.executable, '-c', code]},
        'health': {'id': 'health', 'argv': [sys.executable, '-c', health]},
        'stop': {'id': 'stop', 'argv': ['true']}}])
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path)
    assert report.passed
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / 'service_pid').read_text()), 0)


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux command-local subreaper adapter')
@pytest.mark.asyncio
@pytest.mark.parametrize('exit_code', [None, 0, 7, 143, 'stop'])
async def test_linux_service_start_records_supervised_completion(tmp_path, exit_code):
    service_code = (
        'import os,pathlib,time;pathlib.Path("service_pid").write_text(str(os.getpid()));'
        'exec("while not pathlib.Path(\\\"finish\\\").exists(): time.sleep(0.01)");'
        f'raise SystemExit({exit_code if isinstance(exit_code, int) else 7})'
    )
    check = 'pass'
    if isinstance(exit_code, int):
        check = (
            'import os,pathlib,time\n'
            'pid=int(pathlib.Path("service_pid").read_text())\n'
            'pathlib.Path("finish").touch()\n'
            'while True:\n'
            '    try: os.kill(pid,0)\n'
            '    except ProcessLookupError: break\n'
            '    time.sleep(0.01)\n'
        )
    stop = ['true']
    if exit_code == 'stop':
        stop = [sys.executable, '-c',
                'import os,pathlib,signal;os.kill(int(pathlib.Path("service_pid").read_text()),signal.SIGTERM)']
    manifest = contract([{'id': 'baseline', 'argv': [sys.executable, '-c', check]}], services=[{
        'id': 'foreground', 'start': {'id': 'start', 'argv': [sys.executable, '-c', service_code]},
        'health': {'id': 'health', 'argv': [sys.executable, '-c',
            'import os,pathlib;os.kill(int(pathlib.Path("service_pid").read_text()),0)']},
        'stop': {'id': 'stop', 'argv': stop}}])
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path)
    start = next(result for result in report.results if result.id == 'start')
    assert start.exit_code == (exit_code if isinstance(exit_code, int) else 0)
    failed = exit_code in (7, 143)
    assert start.status == ('fail' if failed else 'pass')
    assert report.passed is not failed
    with pytest.raises(ProcessLookupError):
        os.kill(int((tmp_path / 'service_pid').read_text()), 0)


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux command-local subreaper adapter')
@pytest.mark.asyncio
async def test_optional_command_cannot_hide_unconfirmed_supervisor_cleanup(tmp_path):
    manifest = contract([
        {'id': 'baseline', 'argv': ['true']},
        {'id': 'kill-supervisor', 'required': False, 'argv': [sys.executable, '-c',
            'import os,signal;os.kill(os.getppid(),signal.SIGKILL)']},
    ])
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path)
    assert not report.passed
    assert any(result.required and result.id.startswith('__infrastructure__/cleanup/')
               for result in report.results)


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['', ':/usr/bin::/bin:'])
async def test_activation_never_adds_current_directory_to_path(tmp_path, path):
    import subprocess

    from coordinare_service_inference.environment.runner import manifest_environment

    manifest = contract([{'id': 'base', 'argv': ['true']}],
                        activation={'env': {'PATH': path}, 'path': ['{cache}/bin']})
    expected = [str(tmp_path / 'bin'), *filter(None, path.split(':'))]
    runtime = manifest_environment(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path)
    assert runtime['PATH'].split(':') == expected
    activation = render_activate(manifest, cache_mount_path=str(tmp_path))
    actual = subprocess.check_output(['/bin/bash', '--noprofile', '--norc', '-c',
                                      activation + '\n' + activation + '\nprintf "%s" "$PATH"'],
                                     env={**os.environ, 'BASH_ENV': '', 'ENV': ''}, text=True)
    assert actual.split(':') == expected


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux command-local subreaper adapter')
@pytest.mark.asyncio
async def test_supervisor_completion_cannot_be_changed_by_python_finalization(tmp_path, monkeypatch):
    from coordinare_service_inference.environment import runner

    # Deterministically expose the shutdown window where CPython restores default
    # signal handlers before exit and a concurrent teardown SIGTERM can arrive.
    finalization = '''
import atexit
import os
import signal
def finalization_signal():
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    os.kill(os.getpid(), signal.SIGTERM)
atexit.register(finalization_signal)
'''
    monkeypatch.setattr(runner, '_LINUX_SUPERVISOR', runner._LINUX_SUPERVISOR.replace(
        'from __future__ import annotations', 'from __future__ import annotations\n' + finalization, 1,
    ))
    manifest = contract([{'id': 'baseline', 'argv': ['true']}])
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path)
    assert report.passed
    assert report.results[0].exit_code == 0


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux pidfd child-watcher adapter')
@pytest.mark.asyncio
@pytest.mark.parametrize('exit_code', [0, 7])
async def test_teardown_preserves_exit_before_child_watcher_delivery(exit_code):
    import asyncio

    from coordinare_service_inference.environment.runner import _terminate

    process = await asyncio.create_subprocess_exec(sys.executable, '-c', f'raise SystemExit({exit_code})')
    # Wait for kernel completion without reaping or yielding to the pidfd callback.
    # Popen.send_signal() must not steal that pending status from asyncio.
    os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
    assert process.returncode is None
    await _terminate(process)
    assert process.returncode == exit_code
