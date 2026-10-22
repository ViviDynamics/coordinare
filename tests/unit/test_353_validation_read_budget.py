"""Validation read allowances use existing finite product bounds and real attempt accounting."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest
from scripts import validate_environment_repositories as validation


@pytest.mark.parametrize('allowance', [512, 2048, 8192])
def test_cli_records_explicit_aggregate_read_allowance(tmp_path, monkeypatch, allowance):
    output = tmp_path/'profile'
    monkeypatch.setattr(sys, 'argv', ['validation', 'suite', '--snapshots', str(tmp_path/'snapshots'),
        '--output', str(output), '--image', 'trusted@sha256:'+'a'*64,
        '--model', 'fixture', '--base-url', 'http://localhost:1', '--max-total-reads', str(allowance)])
    monkeypatch.setattr(validation, 'verify_image_code', lambda _image: {})
    async def batch(_args, _image, profile):
        assert profile.max_total_reads == allowance
        return [{'ready_at_validation': False}]
    monkeypatch.setattr(validation, '_batch', batch)
    assert validation.main() == 1
    recorded = json.loads((output/'profile.json').read_text())
    assert recorded['profile']['max_total_reads'] == allowance


@pytest.mark.parametrize('allowance', ['0', '-1', '8193', 'unbounded'])
def test_cli_refuses_invalid_allowance_before_creating_profile(tmp_path, monkeypatch, allowance):
    output = tmp_path/'profile'
    monkeypatch.setattr(sys, 'argv', ['validation', 'suite', '--snapshots', str(tmp_path/'snapshots'),
        '--output', str(output), '--image', 'trusted@sha256:'+'a'*64,
        '--model', 'fixture', '--base-url', 'http://localhost:1', '--max-total-reads', allowance])
    with pytest.raises(SystemExit) as rejected:
        validation.main()
    assert rejected.value.code == 2
    assert not output.exists()


@pytest.mark.asyncio
async def test_configured_allowance_holds_real_attempt_without_skipping_review(tmp_path, monkeypatch):
    from tests.unit.scope_chat_fixture import ScopeChatFixture
    from tests.unit.test_353_bootstrap_pipeline import SOURCE, Proposal

    source = tmp_path/'source'
    source.mkdir()
    subprocess.run(['git', 'init', '-q', str(source)], check=True)
    (source/'guide').write_text(SOURCE)
    subprocess.run(['git', '-C', str(source), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(source), '-c', 'user.name=Fixture', '-c',
        'user.email=fixture@example.invalid', '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'source'], check=True)
    commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    image = 'trusted@sha256:'+'a'*64
    async def identity(_image):
        return image, 'linux/x86_64'
    monkeypatch.setattr('coordinare.services.environment_capabilities._docker_identity', identity)
    output = tmp_path/'profile'
    result = await validation.validate_repository(validation.Snapshot(source, commit, 'fixture', 'suite'), image=image,
        output=output, profile=validation.Profile(model='fixture', base_url='http://localhost:1', max_total_reads=1),
        chat=ScopeChatFixture(Proposal(), 'guide'))
    assert result['status'] == 'held' and result['reason_code'] == 'attempt_read_limit'
    attempt = json.loads((output/'environment-attempt.json').read_text())
    assert attempt['max_reads'] == 1 and attempt['reads'] == 1
    assert result['independent_source_review_invocations'] == 0
    assert not result['installer_ran'] and not result['clean_consumer_ran'] and not result['ready_at_validation']


def test_a_profile_with_a_reused_scope_path_is_recorded_as_json():
    import json
    from pathlib import Path

    from scripts.validate_environment_repositories import Profile, _profile_record

    record = _profile_record(Profile(model='m', base_url='http://x/v1', reuse_scope_from=Path('/runs/v4')))
    assert json.loads(json.dumps(record))['reuse_scope_from'] == '/runs/v4'
