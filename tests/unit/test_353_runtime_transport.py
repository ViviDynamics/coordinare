"""Trusted runtime controls preserve the bootstrap's original aggregate deadline."""
from __future__ import annotations

import json
import os

import pytest


def test_runtime_controls_pop_before_project_children_and_keep_original_deadline(monkeypatch):
    from coordinare_service_inference.environment import runtime_transport as transport

    monkeypatch.setattr(transport.time, 'time', lambda: 1000)
    monkeypatch.setattr(transport.time, 'monotonic', lambda: 50)
    monkeypatch.setenv('COORDINARE_ENV_RUNTIME_EXPIRES_AT', '1120')
    deadline, receipt = transport.consume_runtime_controls(required=True, install=True)
    assert deadline == 170 and receipt is None
    assert 'COORDINARE_ENV_RUNTIME_EXPIRES_AT' not in os.environ


@pytest.mark.parametrize('expiry', [None, 'nan', 'inf', '-inf', '1000', 'false'])
def test_runtime_missing_invalid_or_expired_budget_is_refused(monkeypatch, expiry):
    from coordinare_service_inference.environment import runtime_transport as transport

    monkeypatch.setattr(transport.time, 'time', lambda: 1000)
    monkeypatch.delenv('COORDINARE_ENV_RUNTIME_EXPIRES_AT', raising=False)
    if expiry is not None:
        monkeypatch.setenv('COORDINARE_ENV_RUNTIME_EXPIRES_AT', expiry)
    with pytest.raises((ValueError, TimeoutError)):
        transport.consume_runtime_controls(required=True, install=True)


def test_consumer_receipt_comes_from_bounded_trusted_transport(monkeypatch):
    from coordinare_service_inference.environment import runtime_transport as transport

    raw = {'schema_version': 1, 'manifest_digest': '1'*64, 'source_digest': '2'*64,
           'runtime_digest': '3'*64, 'sealed': True,
           'artifacts': [{'path': 'installed.qcow2', 'sha256': '4'*64, 'bytes': 100}]}
    monkeypatch.setattr(transport.time, 'time', lambda: 1000)
    monkeypatch.setenv('COORDINARE_ENV_RUNTIME_EXPIRES_AT', '1120')
    monkeypatch.setenv('COORDINARE_ENV_RUNTIME_ARTIFACTS', json.dumps(raw))
    _, receipt = transport.consume_runtime_controls(required=True, install=False)
    assert receipt.model_dump() == raw
    assert 'COORDINARE_ENV_RUNTIME_ARTIFACTS' not in os.environ


@pytest.mark.parametrize('raw', [None, '{}', 'x'*65537], ids=['missing', 'invalid', 'oversized'])
def test_consumer_missing_or_unbounded_receipt_fails_closed(monkeypatch, raw):
    from coordinare_service_inference.environment import runtime_transport as transport

    monkeypatch.setattr(transport.time, 'time', lambda: 1000)
    monkeypatch.setenv('COORDINARE_ENV_RUNTIME_EXPIRES_AT', '1120')
    monkeypatch.delenv('COORDINARE_ENV_RUNTIME_ARTIFACTS', raising=False)
    if raw is not None:
        monkeypatch.setenv('COORDINARE_ENV_RUNTIME_ARTIFACTS', raw)
    with pytest.raises(ValueError):
        transport.consume_runtime_controls(required=True, install=False)


def test_native_controls_do_not_create_runtime_or_reset_a_deadline(monkeypatch):
    from coordinare_service_inference.environment.runtime_transport import consume_runtime_controls

    monkeypatch.delenv('COORDINARE_ENV_RUNTIME_ARTIFACTS', raising=False)
    monkeypatch.delenv('COORDINARE_ENV_RUNTIME_EXPIRES_AT', raising=False)
    assert consume_runtime_controls(required=False, install=False) == (None, None)
    monkeypatch.setenv('COORDINARE_ENV_RUNTIME_EXPIRES_AT', '1120')
    with pytest.raises(ValueError):
        consume_runtime_controls(required=False, install=False)


def test_verifier_cli_passes_trusted_deadline_to_runtime(monkeypatch, capsys):
    import sys
    from types import SimpleNamespace

    from coordinare_service_inference.environment import runner, runtime_transport

    monkeypatch.setattr(sys, 'argv', ['runner', '--manifest', '{}', '--cache', '/cache',
                                   '--repo', '/repo', '--scratch', '/scratch', '--install'])
    monkeypatch.setattr(runner.EnvironmentManifest, 'model_validate', lambda data: SimpleNamespace(runtime=object()))
    monkeypatch.setattr(runtime_transport.time, 'time', lambda: 1000)
    monkeypatch.setattr(runtime_transport.time, 'monotonic', lambda: 50)
    monkeypatch.setenv('COORDINARE_ENV_RUNTIME_EXPIRES_AT', '1120')
    observed = {}
    async def execute(manifest, **kwargs):
        observed.update(kwargs)
        assert 'COORDINARE_ENV_RUNTIME_EXPIRES_AT' not in os.environ
        return SimpleNamespace(passed=True, model_dump_json=lambda: '{}')
    monkeypatch.setattr(runner, 'run_manifest', execute)
    with pytest.raises(SystemExit) as exit_info:
        runner.main()
    assert exit_info.value.code == 0
    assert observed['runtime_deadline'] == 170
    assert observed['runtime_artifacts'] is None
    assert capsys.readouterr().out == '{}\n'


def test_source_installer_preserves_the_same_original_deadline(monkeypatch):
    import io
    import sys
    from types import SimpleNamespace

    from coordinare_service_inference.environment import execution_source_install as entry
    from coordinare_service_inference.environment import runtime_transport

    monkeypatch.setattr(sys, 'argv', ['installer', '--cache', '/cache', '--repo', '/repo', '--scratch', '/scratch'])
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(b'{"manifest":{},"execution_source":{}}')))
    manifest = SimpleNamespace(runtime=object(), source_commit='a'*40, inputs={})
    source = SimpleNamespace(digest=lambda: 'b'*64)
    monkeypatch.setattr(entry.EnvironmentManifest, 'model_validate', lambda data: manifest)
    monkeypatch.setattr(entry.ExecutionSourceIdentity, 'model_validate', lambda data: source)
    async def identity(*args, **kwargs):
        return source
    monkeypatch.setattr(entry, 'checkout_identity', identity)
    monkeypatch.setattr(runtime_transport.time, 'time', lambda: 1000)
    monkeypatch.setattr(runtime_transport.time, 'monotonic', lambda: 50)
    monkeypatch.setenv('COORDINARE_ENV_RUNTIME_EXPIRES_AT', '1120')
    observed = {}
    async def execute(manifest, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(passed=True, model_dump_json=lambda: '{}')
    monkeypatch.setattr(entry, 'run_manifest', execute)
    with pytest.raises(SystemExit) as exit_info:
        entry.main()
    assert exit_info.value.code == 0
    assert observed['runtime_deadline'] == 170
    assert observed['execution_source_digest'] == 'b'*64
