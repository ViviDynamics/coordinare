"""The trusted runner generates local service credentials per run and never lets them escape."""
from __future__ import annotations

import sys

import pytest
from coordinare_service_inference.environment.models import EnvironmentManifest
from coordinare_service_inference.environment.runner import (
    local_credential_values,
    manifest_environment,
    run_manifest,
)

# The service is scoped away so the check runs on every platform; credentials still bind to it.
SERVICE = {'id': 'postgres', 'required': True,
           **{stage: {'id': f'postgres-{stage}', 'argv': ['true'], 'scope': ['database']}
              for stage in ('start', 'health', 'stop')}}
ECHO = {'id': 'echo', 'argv': [sys.executable, '-c', 'import os; print(os.environ["PGPASSWORD"], os.environ["DSN"]); raise SystemExit(1)'],
        'env': {'PGPASSWORD': '${PGPASSWORD}', 'DSN': 'postgres://postgres:${PGPASSWORD}@localhost/elm'}, 'required': True}


def manifest(checks=(ECHO,)):
    return EnvironmentManifest.model_validate({
        'schema_version': 1, 'symphony_name': 'fixture', 'source_commit': 'a'*40, 'inputs': {}, 'requirements': [],
        'install': [], 'checks': list(checks), 'activation': {}, 'services': [SERVICE],
        'empty_reason': 'uses base image', 'policy_version': '351-v1',
        'local_credentials': [{'name': 'PGPASSWORD', 'service': 'postgres', 'purpose': 'test database password'}]})


def test_values_are_fresh_per_run_and_never_in_the_manifest():
    first = local_credential_values(manifest(), supplied=())
    second = local_credential_values(manifest(), supplied=())
    assert set(first) == {'PGPASSWORD'} and first['PGPASSWORD'] != second['PGPASSWORD']
    assert len(first['PGPASSWORD']) >= 40
    assert first['PGPASSWORD'] not in manifest().model_dump_json()


def test_an_operator_secret_with_the_same_name_conflicts():
    with pytest.raises(ValueError, match='local_credential_conflict'):
        local_credential_values(manifest(), supplied=['PGPASSWORD'])


def test_activation_exposes_values_as_declared_secrets(tmp_path):
    values = local_credential_values(manifest(), supplied=())
    env = manifest_environment(manifest(), cache=tmp_path, repo=tmp_path, scratch=tmp_path, credentials=values)
    assert env['PGPASSWORD'] == values['PGPASSWORD']
    assert 'PGPASSWORD' in env['COORDINARE_ENV_SECRET_NAMES']


@pytest.mark.asyncio
async def test_check_output_redacts_generated_values(tmp_path):
    report = await run_manifest(manifest(), cache=tmp_path, repo=tmp_path, scratch=tmp_path, scope='app')
    [result] = [r for r in report.results if r.id == 'echo']
    # The password and the whole credential-bearing DSN are both hidden.
    assert result.status == 'fail' and result.output == '[REDACTED] [REDACTED]\n'


@pytest.mark.asyncio
async def test_operator_conflict_fails_before_any_command(tmp_path, monkeypatch):
    marker = tmp_path / 'ran'
    touch = {'id': 'touch', 'argv': [sys.executable, '-c', f'open({str(marker)!r}, "w")'],
             'env': {'PGPASSWORD': '${PGPASSWORD}'}, 'required': True}
    monkeypatch.setenv('PGPASSWORD', 'operator-value')
    report = await run_manifest(manifest([touch]), cache=tmp_path, repo=tmp_path, scratch=tmp_path, scope='app',
                                secret_names=['PGPASSWORD'])
    assert not report.passed and not marker.exists()
    [result] = [r for r in report.results if r.id == 'local_credentials']
    assert result.required and result.status == 'fail' and 'operator-value' not in result.output
