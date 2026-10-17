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
        "source_commit": "abc",
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
    assert report.passed
    assert marker.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


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
