"""Standalone entrypoints cancel work and finish owned cleanup on termination."""
from __future__ import annotations

import contextlib
import json
import os
import selectors
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.env_manifest_fixture import reviewed_metadata

_PROGRAM = '''
import asyncio, importlib, json, pathlib, sys
name, directory, mode = sys.argv[1:]
module = importlib.import_module('coordinare_service_inference.environment.' + name)
root = pathlib.Path(directory)
async def work(*args, **kwargs):
    child = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(60)',
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    (root/'child.pid').write_text(str(child.pid))
    (root/'control-key').write_text('ephemeral fixture')
    print('ready', flush=True)
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        if mode != 'swallow':
            raise
    finally:
        print('cleaning', flush=True)
        await asyncio.sleep(0.15)
        child.kill()
        await child.wait()
        (root/'control-key').unlink()
        (root/'cleaned').write_text('reaped')
    from types import SimpleNamespace
    return SimpleNamespace(passed=True, model_dump_json=lambda: 'success')
if name == 'observation':
    module.observe_private = work
    sys.argv = ['observation']
else:
    module.run_manifest = work
    sys.argv = [name, '--repo', directory, '--cache', directory, '--scratch', directory]
    if name == 'runner':
        sys.argv += ['--manifest-stdin']
    else:
        from coordinare_service_inference.environment.execution_source import ExecutionSourceIdentity
        async def checkout(*args, **kwargs):
            return ExecutionSourceIdentity.model_validate(json.loads((root/'source.json').read_text()))
        module.checkout_identity = checkout
module.main()
'''


def _line(process: subprocess.Popen[str]) -> str:
    assert process.stdout is not None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(5), 'entrypoint did not reach lifecycle barrier'
    return process.stdout.readline().strip()


@pytest.mark.parametrize('entrypoint', ['runner', 'execution_source_install', 'observation'])
@pytest.mark.parametrize('mode', ['termination', 'repeat', 'swallow'])
def test_termination_reaps_child_and_removes_keys_before_failing(tmp_path: Path, entrypoint: str, mode: str) -> None:
    metadata = reviewed_metadata()
    (tmp_path/'source.json').write_text(json.dumps(metadata['environment_execution_source']))
    if entrypoint == 'observation':
        payload = {'runtime': {}}
    elif entrypoint == 'runner':
        payload = metadata['environment_manifest']
    else:
        payload = {'manifest': metadata['environment_manifest'], 'execution_source': metadata['environment_execution_source']}
    with subprocess.Popen([sys.executable, '-c', _PROGRAM, entrypoint, str(tmp_path), mode],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        try:
            assert process.stdin is not None
            process.stdin.write(json.dumps(payload))
            process.stdin.close()
            process.stdin = None
            assert _line(process) == 'ready'
            process.send_signal(signal.SIGTERM)
            assert _line(process) == 'cleaning'
            if mode == 'repeat':
                process.send_signal(signal.SIGTERM)
            output, _ = process.communicate(timeout=5)
            assert process.returncode == 128 + signal.SIGTERM
            assert output == ''  # No success evidence on cancellation.
            assert (tmp_path/'cleaned').read_text() == 'reaped'
            assert not (tmp_path/'control-key').exists()
            with pytest.raises(ProcessLookupError):
                os.kill(int((tmp_path/'child.pid').read_text()), 0)
        finally:
            if process.poll() is None:
                process.kill()
            pid = tmp_path/'child.pid'
            if pid.exists():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(pid.read_text()), signal.SIGKILL)
            process.communicate(timeout=5)


@pytest.mark.parametrize('failure', [False, True])
def test_cli_restores_callers_signal_handler_after_success_or_error(failure: bool) -> None:
    from coordinare_service_inference.environment.runtime_cli import run_cli

    previous = signal.getsignal(signal.SIGTERM)

    async def operation() -> int:
        if failure:
            raise ValueError('execution failed')
        return 7

    if failure:
        with pytest.raises(ValueError, match='execution failed'):
            run_cli(operation())
    else:
        assert run_cli(operation()) == 7
    assert signal.getsignal(signal.SIGTERM) == previous
