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
@pytest.mark.parametrize('exit_code', [0, 7, 143])
@pytest.mark.timeout(10)
async def test_linux_teardown_preserves_exit_before_child_watcher_runs(exit_code):
    import asyncio
    import contextlib
    import signal

    from coordinare_service_inference.environment.runner import _terminate

    process = await asyncio.create_subprocess_exec(
        sys.executable, '-c',
        'import sys;sys.stdin.buffer.read(1);raise SystemExit(int(sys.argv[1]))',
        str(exit_code), stdin=asyncio.subprocess.PIPE, start_new_session=True,
    )
    try:
        assert process.stdin is not None
        process.stdin.write(b'1')
        # Observe exit without reaping or yielding to the pidfd watcher. Teardown
        # now runs in the exact window that Popen.send_signal().poll() can steal.
        exited = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
        assert exited.si_status == exit_code
        assert process.returncode is None
        await _terminate(process)
        assert process.returncode == exit_code
    finally:
        if process.stdin is not None:
            process.stdin.close()
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(process.pid, signal.SIGKILL)
            await process.wait()


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


@pytest.mark.asyncio
async def test_an_install_step_that_breaks_the_environment_is_named(tmp_path):
    """Dettmore e2e-39: an install step left the activated environment unable to run a shell, and nothing named it."""
    from coordinare_service_inference.environment.runner import UNHEALTHY

    broken = "mkdir -p {cache}/bin && printf '#!/bin/sh\\necho shell is broken >&2\\nexit 3\\n' > {cache}/bin/sh && chmod +x {cache}/bin/sh"
    manifest = contract(
        [{"id": "check", "argv": ["true"]}],
        install=[{"id": "fine", "argv": ["sh", "-c", "mkdir -p {cache}/tools"]},
                 {"id": "breaks-shell", "argv": ["sh", "-c", broken], "depends_on": ["fine"]}],
        activation={"path": ["{cache}/bin"]},
    )
    report = await run_manifest(manifest, cache=tmp_path / "cache", repo=tmp_path, scratch=tmp_path, install=True)
    results = {result.id: result for result in report.results}
    assert results["fine"].status == "pass"
    assert results["breaks-shell"].status == "fail"
    assert results["breaks-shell"].output.startswith(UNHEALTHY + "shell is broken")
    assert "environment-health" not in results and not report.passed


@pytest.mark.asyncio
async def test_error_lines_before_the_kept_tail_are_reported(tmp_path, monkeypatch):
    """Dettmore e2e-45: "OpenSSL is not available" scrolled out behind a rubygems backtrace, hiding the cause."""
    from coordinare_service_inference.environment.runner import OUTPUT_LIMIT

    monkeypatch.setenv('TEST_SECRET', 'sensitive-value-654')
    script = ('import sys\n'
              'print("configure ok\\n" * 50)\n'
              'print("ERROR:  While executing gem ... (Gem::Exception)")\n'
              'print("    OpenSSL is not available. Install OpenSSL and rebuild Ruby (sensitive-value-654)")\n'
              'for i in range(200): print(f"\\t/devenv/ruby/lib/rubygems/request.rb:{i}:in \'Gem::Request#fetch\'")\n'
              'sys.exit(1)\n')
    manifest = contract([{'id': 'ruby', 'argv': [sys.executable, '-c', script]},
                         {'id': 'short', 'argv': [sys.executable, '-c', 'print("error: small")']}])
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path)
    ruby, short = report.results[0], report.results[1]
    assert ruby.output_truncated
    assert len(ruby.output.encode()) <= OUTPUT_LIMIT
    assert 'OpenSSL is not available' in ruby.output
    assert 'ERROR:  While executing gem' in ruby.output
    assert 'sensitive-value-654' not in ruby.output
    assert ruby.output.rstrip().endswith("'Gem::Request#fetch'")
    assert 'error lines from earlier output' not in short.output


@pytest.mark.asyncio
async def test_a_failed_command_gets_the_tail_of_the_build_log_it_names(tmp_path, monkeypatch):
    """Dettmore e2e-45..51: ruby-build printed only "See the full build log at /tmp/ruby-build.N.log"; the cause stayed
    in that file for two repair generations."""
    import tempfile

    from coordinare_service_inference.environment.runner import FAILED_OUTPUT_LIMIT

    monkeypatch.setenv('TEST_SECRET', 'sensitive-value-777')
    with tempfile.NamedTemporaryFile('w', dir='/tmp', prefix='ruby-build.', suffix='.log', delete=False) as handle:
        handle.write('compiling\n' * 2000 + 'psych: Could not be configured (sensitive-value-777)\n')
        log = handle.name
    try:
        failing = (f'import sys\nprint("BUILD FAILED (Debian 13 using ruby-build)")\n'
                   f'print("See the full build log at {log}")\nsys.exit(1)\n')
        passing = f'print("See the full build log at {log}")\n'
        manifest = contract([{'id': 'ruby', 'argv': [sys.executable, '-c', failing]},
                             {'id': 'ok', 'argv': [sys.executable, '-c', passing]}])
        report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path)
    finally:
        os.unlink(log)
    ruby, ok = report.results
    assert 'psych: Could not be configured' in ruby.output and 'BUILD FAILED' in ruby.output
    assert 'sensitive-value-777' not in ruby.output and len(ruby.output.encode()) <= FAILED_OUTPUT_LIMIT
    assert 'tail of the build log' not in ok.output


@pytest.mark.asyncio
async def test_a_failed_command_gets_the_error_lines_of_every_log_its_build_log_names(tmp_path, monkeypatch):
    """Dettmore e2e-54: the build log named ext/openssl/mkmf.log (relative to the source tree) and others whose
    lines say why each extension was not configured; only the build log's own tail reached the repair."""
    import shutil
    import tempfile
    from pathlib import Path

    from coordinare_service_inference.environment.runner import FAILED_OUTPUT_LIMIT

    monkeypatch.setenv('TEST_SECRET', 'sensitive-value-888')
    build = Path(tempfile.mkdtemp(dir='/tmp', prefix='lang-build.'))
    try:
        source = build / 'lang-1.2'
        (source / 'ext' / 'tls').mkdir(parents=True)
        (source / 'ext' / 'yaml').mkdir(parents=True)
        (source / 'ext' / 'tls' / 'mkmf.log').write_text(
            'checking for tls.h... yes\n' * 500 + '/usr/bin/ld: cannot find -ltls (sensitive-value-888)\n')
        (source / 'ext' / 'yaml' / 'mkmf.log').write_text('conftest.c:1:10: fatal error: yaml.h: No such file\n')
        log = build / 'build.log'
        log.write_text('make: entering\n' * 3000 + f'tls:\n\t{source}/ext/tls/extconf.rb:26: library could not be found\n'
                       '\tCheck ext/tls/mkmf.log for more details.\n'
                       f'yaml:\n\tCheck {source}/ext/yaml/mkmf.log for more details.\n')
        failing = f'import sys\nprint("BUILD FAILED")\nprint("See the full build log at {log}")\nsys.exit(1)\n'
        manifest = contract([{'id': 'lang', 'argv': [sys.executable, '-c', failing]}])
        report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path)
    finally:
        shutil.rmtree(build)
    output = report.results[0].output
    assert output.startswith('[error lines from the logs this output names]')
    assert 'cannot find -ltls' in output and 'fatal error: yaml.h: No such file' in output
    assert 'library could not be found' in output and output.rstrip().endswith('See the full build log at ' + str(log))
    assert 'sensitive-value-888' not in output and len(output.encode()) <= FAILED_OUTPUT_LIMIT


def _outside_roots():
    """A directory outside /tmp, the cache and the scratch root: pytest's tmp_path is under /tmp on Linux."""
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp(dir=Path(__file__).resolve().parent, prefix='.outside-roots-'))


@pytest.mark.asyncio
async def test_a_named_log_outside_the_allowed_roots_is_never_read(tmp_path):
    import shutil

    elsewhere = _outside_roots()
    try:
        outside = elsewhere / 'secret.log'
        outside.write_text('error: private detail\n')
        failing = f'import sys\nprint("see {outside}")\nsys.exit(1)\n'
        manifest = contract([{'id': 'x', 'argv': [sys.executable, '-c', failing]}])
        report = await run_manifest(manifest, cache=tmp_path / 'cache', repo=tmp_path, scratch=tmp_path / 'scratch')
    finally:
        shutil.rmtree(elsewhere)
    assert 'private detail' not in report.results[0].output


@pytest.mark.asyncio
async def test_a_failed_service_step_gets_the_log_its_start_command_writes(tmp_path):
    """Dettmore e2e-54: health said only "maxio-mock not ready"; the reason was in the log the start redirected to."""
    manifest = contract([{"id": "usable", "argv": ["true"]}], services=[{
        "id": "mock",
        "start": {"id": "start-mock", "argv": ["sh", "-c", "echo 'bind failed: port in use' > {scratch}/mock.log"]},
        "health": {"id": "health-mock", "argv": ["sh", "-c", "echo 'mock not ready'; exit 1"]},
        "stop": {"id": "stop-mock", "argv": ["true"]},
    }])
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path)
    health = next(result for result in report.results if result.id == "health-mock")
    assert health.status == "fail"
    assert health.output.startswith("mock not ready\n[tail of ") and "bind failed: port in use" in health.output


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "linux", reason="only the Linux supervisor reports a started service's exit")
async def test_a_service_that_fails_after_starting_keeps_its_later_output(tmp_path):
    """Dettmore e2e-54: start-maxio exited 1 with no output; only its first 0.1 s had been kept."""
    late = "import sys,time\ntime.sleep(0.4)\nprint('late failure reason', flush=True)\nsys.exit(1)\n"
    manifest = contract([{"id": "usable", "argv": ["true"]}], services=[{
        "id": "mock",
        "start": {"id": "start-mock", "argv": [sys.executable, "-c", late]},
        "health": {"id": "health-mock", "argv": [sys.executable, "-c", "import time\ntime.sleep(1)\nraise SystemExit(1)"]},
        "stop": {"id": "stop-mock", "argv": ["true"]},
    }])
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path)
    start = next(result for result in report.results if result.id == "start-mock")
    assert start.status == "fail" and "late failure reason" in start.output


@pytest.mark.asyncio
async def test_services_never_start_when_a_required_install_failed(tmp_path):
    """Dettmore e2e-54: with install-ruby failed, the services still started and their failures read as faults of
    their own. Each service step is not_run and names the failed install; the start never runs."""
    manifest = contract([{"id": "usable", "argv": ["true"]}],
                        install=[{"id": "install-lang", "argv": ["false"]}, {"id": "install-tool", "argv": ["true"]}],
                        services=[{"id": "mock",
                                   "start": {"id": "start-mock", "argv": ["sh", "-c", "touch {scratch}/started"]},
                                   "health": {"id": "health-mock", "argv": ["true"]},
                                   "stop": {"id": "stop-mock", "argv": ["true"]}}])
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path, install=True)
    results = {result.id: result for result in report.results}
    assert results["install-lang"].status == "fail" and results["install-tool"].status == "pass"
    for step in ("start-mock", "health-mock", "stop-mock"):
        assert results[step].status == "not_run" and results[step].output == "required install did not pass: install-lang"
    assert results["usable"].status == "not_run" and not (tmp_path / "started").exists() and not report.passed


@pytest.mark.asyncio
async def test_a_log_reached_through_a_symlinked_parent_is_never_read(tmp_path):
    import shutil

    outside = _outside_roots()
    try:
        (outside / 'secret.log').write_text('error: private detail\n')
        scratch = tmp_path / 'scratch'
        scratch.mkdir()
        (scratch / 'link').symlink_to(outside)
        failing = f'import sys\nprint("see {scratch}/link/secret.log")\nsys.exit(1)\n'
        manifest = contract([{'id': 'x', 'argv': [sys.executable, '-c', failing]}])
        report = await run_manifest(manifest, cache=tmp_path / 'cache', repo=tmp_path, scratch=scratch)
    finally:
        shutil.rmtree(outside)
    assert 'private detail' not in report.results[0].output


@pytest.mark.asyncio
async def test_an_out_of_scope_install_never_blocks_the_services(tmp_path):
    manifest = contract([{"id": "usable", "argv": ["true"], "scope": ["unit"]}],
                        install=[{"id": "install-other", "argv": ["false"], "scope": ["e2e"]}],
                        services=[{"id": "mock", "start": {"id": "start-mock", "argv": ["true"]},
                                   "health": {"id": "health-mock", "argv": ["true"]},
                                   "stop": {"id": "stop-mock", "argv": ["true"]}}])
    report = await run_manifest(manifest, cache=tmp_path, repo=tmp_path, scratch=tmp_path, install=True, scope="unit")
    results = {result.id: result for result in report.results}
    assert results["install-other"].output == "outside requested scope"
    assert results["start-mock"].status == "pass" and results["usable"].status == "pass"
