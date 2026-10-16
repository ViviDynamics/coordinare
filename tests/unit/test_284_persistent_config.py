from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from coordinare.config import expand_config_text
from coordinare.config_descriptors import SECRET_MASK
from coordinare.services import config_seed
from coordinare.services.config_write_service import compute_content_hash
from tests.unit.test_147_helm_deployment import _container, _one, _render
from tests.unit.test_221_symphony_editor import _BASE, _client

SEED = 'github_org: acme\ngithub_token: ${GITHUB_TOKEN}\npoll_interval_seconds: 30\n'
LIVE_EDIT = SEED.replace('30', '60')


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    seed = tmp_path / 'chart.yaml'
    seed.write_text(SEED)
    return seed, tmp_path / 'config' / 'config.yaml'


def test_first_boot_imports_raw_config_and_records_seed(tmp_path: Path) -> None:
    seed, live = _paths(tmp_path)
    config_seed.seed_config(seed, live)
    assert yaml.safe_load(live.read_text()) == yaml.safe_load(SEED)
    assert '${GITHUB_TOKEN}' in live.read_text()
    assert config_seed.seed_status(seed, live)['chart_changed'] is False
    assert yaml.safe_load((live.parent / 'seed.yaml').read_text())['seed_hash'] == compute_content_hash(seed)


def test_restart_and_upgrade_preserve_dashboard_edits(tmp_path: Path) -> None:
    seed, live = _paths(tmp_path)
    config_seed.seed_config(seed, live)
    live.write_text(LIVE_EDIT)
    config_seed.seed_config(seed, live)
    assert live.read_text() == LIVE_EDIT
    seed.write_text(SEED.replace('30', '90'))
    config_seed.seed_config(seed, live)
    assert live.read_text() == LIVE_EDIT
    status = config_seed.seed_status(seed, live)
    assert status['chart_changed'] is True
    assert status['changed_sections'] == ['poll_interval_seconds']
    assert status['live_diff_sections'] == ['poll_interval_seconds']
    assert 'chart values changed since this config was seeded' in status['message']
    assert '90' not in json.dumps(status)


def test_dashboard_edit_alone_is_not_chart_drift(tmp_path: Path) -> None:
    seed, live = _paths(tmp_path)
    config_seed.seed_config(seed, live)
    live.write_text(LIVE_EDIT)
    status = config_seed.seed_status(seed, live)
    assert status['chart_changed'] is False
    assert status['live_diff_sections'] == ['poll_interval_seconds']


def test_reset_marker_explicitly_adopts_new_seed_once(tmp_path: Path) -> None:
    seed, live = _paths(tmp_path)
    config_seed.seed_config(seed, live)
    live.write_text(LIVE_EDIT)
    seed.write_text(SEED.replace('acme', 'new-org'))
    marker = live.parent / 'reseed'
    marker.touch()
    config_seed.seed_config(seed, live)
    assert yaml.safe_load(live.read_text()) == yaml.safe_load(seed.read_text())
    assert not marker.exists()
    assert config_seed.seed_status(seed, live)['chart_changed'] is False
    live.write_text(LIVE_EDIT)
    config_seed.seed_config(seed, live)
    assert live.read_text() == LIVE_EDIT


@pytest.mark.parametrize('metadata', [None, 'broken: [', 'seed_hash: bogus', '[]'])
def test_lost_metadata_never_rebaselines_or_overwrites(tmp_path: Path, metadata: str | None) -> None:
    seed, live = _paths(tmp_path)
    live.parent.mkdir()
    live.write_text(LIVE_EDIT)
    if metadata is not None:
        (live.parent / 'seed.yaml').write_text(metadata)
    config_seed.seed_config(seed, live)
    assert live.read_text() == LIVE_EDIT
    status = config_seed.seed_status(seed, live)
    assert status['chart_changed'] is None
    assert 'unknown' in status['message'].lower()


def test_failed_reset_keeps_live_file_and_marker(tmp_path: Path) -> None:
    seed, live = _paths(tmp_path)
    config_seed.seed_config(seed, live)
    live.write_text(LIVE_EDIT)
    marker = live.parent / 'reseed'
    marker.touch()
    with (
        patch('coordinare.services.config_write_service.os.replace', side_effect=OSError('disk full')),
        pytest.raises(OSError, match='disk full'),
    ):
        config_seed.seed_config(seed, live)
    assert live.read_text() == LIVE_EDIT
    assert marker.exists()
    assert not list(live.parent.glob('*.tmp'))


@pytest.mark.parametrize('extra', [
    'github_token: literal-secret\n',
    'webhooks:\n  secret: literal-secret\n',
    'symphonies:\n  - name: demo\n    overrides:\n      github_token: literal-secret\n',
    'performer_endpoints:\n  - env:\n      API_KEY: literal-secret\n',
])
def test_seed_refuses_literal_secrets_without_persisting_them(tmp_path: Path, extra: str) -> None:
    seed, live = _paths(tmp_path)
    seed.write_text(extra)
    with pytest.raises(ValueError, match='placeholder') as error:
        config_seed.seed_config(seed, live)
    assert 'literal-secret' not in str(error.value)
    assert not live.exists()
    assert not (live.parent / 'seed.yaml').exists()


def test_drift_status_is_secret_free_and_tolerates_bad_seed(tmp_path: Path) -> None:
    seed, live = _paths(tmp_path)
    config_seed.seed_config(seed, live)
    seed.write_text('github_token: literal-secret\n')
    assert 'literal-secret' not in json.dumps(config_seed.seed_status(seed, live))
    seed.write_text('invalid: [')
    assert config_seed.seed_status(seed, live)['chart_changed'] is None


def test_helm_defaults_to_state_config_with_nonroot_seed_init() -> None:
    objects = _render()
    pod = _one(objects, 'StatefulSet')['spec']['template']['spec']
    container = _container(objects)
    assert container['args'] == ['--config', '/var/lib/coordinare/config/config.yaml']
    init = pod['initContainers'][0]
    assert init['name'] == 'seed-config'
    assert init['image'] == container['image']
    assert init['args'][-2:] == ['/etc/coordinare/config.yaml', '/var/lib/coordinare/config/config.yaml']
    assert init['securityContext'] == container['securityContext']
    assert 'envFrom' not in init
    mounts = {m['name']: m for m in init['volumeMounts']}
    assert mounts['config']['readOnly'] is True
    assert not mounts['state'].get('readOnly', False)
    assert len(_one(objects, 'StatefulSet')['spec']['volumeClaimTemplates']) == 1
    env = {item['name']: item.get('value') for item in container['env']}
    assert env['COORDINARE_CONFIG_SEED'] == '/etc/coordinare/config.yaml'


def test_helm_disabled_preserves_configmap_behavior_and_claim_template() -> None:
    objects = _render(**{'configPersistence.enabled': False})
    container = _container(objects)
    pod = _one(objects, 'StatefulSet')['spec']['template']['spec']
    assert container['args'] == ['--config', '/etc/coordinare/config.yaml']
    assert not pod.get('initContainers')
    assert 'COORDINARE_CONFIG_SEED' not in {e['name'] for e in container['env']}
    assert _one(objects, 'StatefulSet')['spec']['volumeClaimTemplates'] == _one(_render(), 'StatefulSet')['spec']['volumeClaimTemplates']


def test_helm_custom_state_path_and_image_digest() -> None:
    objects = _render(**{'state.mountPath': '/data', 'image.digest': 'sha256:abc'})
    pod = _one(objects, 'StatefulSet')['spec']['template']['spec']
    assert _container(objects)['args'][-1] == '/data/config/config.yaml'
    assert pod['initContainers'][0]['args'][-1] == '/data/config/config.yaml'
    assert pod['initContainers'][0]['image'] == _container(objects)['image']


def _dashboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    seed, live = _paths(tmp_path)
    raw = {**_BASE, 'github_token': '${GITHUB_TOKEN}'}
    seed.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv('GITHUB_TOKEN', 'ghp_runtime_secret')
    monkeypatch.setenv('COORDINARE_CONFIG_SEED', str(seed))
    config_seed.seed_config(seed, live)
    client, daemon = _client(live, raw)
    return seed, live, client, daemon


def test_snapshot_exposes_drift_summary_without_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed, live, client, _ = _dashboard(tmp_path, monkeypatch)
    seed.write_text(seed.read_text().replace('acme', 'another-org'))
    response = client.get('/api/config/all')
    assert response.status_code == 200
    assert response.json()['config_seed']['chart_changed'] is True
    assert response.json()['config_seed']['changed_sections'] == ['github_org']
    assert 'ghp_runtime_secret' not in response.text
    assert 'another-org' not in response.text
    assert str(live) not in response.text


def test_masked_symphony_save_preserves_placeholder_across_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed, live, client, _ = _dashboard(tmp_path, monkeypatch)
    raw = yaml.safe_load(live.read_text())
    raw['symphonies'][0]['overrides']['github_token'] = '${GITHUB_TOKEN}'
    live.write_text(yaml.safe_dump(raw))
    client, _ = _client(live, yaml.safe_load(expand_config_text(live.read_text())))
    before = client.get('/api/symphonies/demo')
    response = client.put('/api/symphonies/demo', headers={'If-Match': before.headers['ETag']},
                          json={'overrides': {'github_token': SECRET_MASK, 'max_concurrent_cards': 4}})
    assert response.status_code == 200, response.text
    assert '${GITHUB_TOKEN}' in live.read_text()
    assert 'ghp_runtime_secret' not in live.read_text()
    config_seed.seed_config(seed, live)
    assert yaml.safe_load(live.read_text())['symphonies'][0]['overrides']['max_concurrent_cards'] == 4


def test_persisted_dashboard_refuses_literal_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, live, client, _ = _dashboard(tmp_path, monkeypatch)
    original = live.read_bytes()
    before = client.get('/api/symphonies/demo')
    response = client.put('/api/symphonies/demo', headers={'If-Match': before.headers['ETag']},
                          json={'overrides': {'github_token': 'ghp_literal_secret'}})
    assert response.status_code == 422
    assert 'placeholder' in response.text
    assert 'ghp_literal_secret' not in response.text
    assert live.read_bytes() == original


def test_seed_init_command_runs_without_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed, live = _paths(tmp_path)
    monkeypatch.delenv('GITHUB_TOKEN', raising=False)
    result = subprocess.run(
        [sys.executable, '-m', 'coordinare.services.config_seed', str(seed), str(live)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert yaml.safe_load(live.read_text())['github_token'] == '${GITHUB_TOKEN}'


def test_seed_null_credentials_remain_null(tmp_path: Path) -> None:
    seed, live = _paths(tmp_path)
    seed.write_text('github_token: null\nwebhooks:\n  secret: null\n')
    config_seed.seed_config(seed, live)
    assert yaml.safe_load(live.read_text()) == {'github_token': None, 'webhooks': {'secret': None}}


def test_invalid_secret_reset_retains_edits_and_marker(tmp_path: Path) -> None:
    seed, live = _paths(tmp_path)
    config_seed.seed_config(seed, live)
    live.write_text(LIVE_EDIT)
    seed.write_text('github_token: literal-secret\n')
    marker = live.parent / 'reseed'
    marker.touch()
    with pytest.raises(ValueError, match='placeholder'):
        config_seed.seed_config(seed, live)
    assert live.read_text() == LIVE_EDIT
    assert marker.exists()


def test_section_save_is_durable_concurrent_and_triggers_existing_reload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed, live, client, daemon = _dashboard(tmp_path, monkeypatch)
    baseline = compute_content_hash(live)
    body = {'store': 'config_yaml', 'section': 'global', 'base_hash': baseline,
            'changes': {'poll_interval_seconds': 75}}
    response = client.put('/api/config/section/global', json=body)
    assert response.status_code == 200, response.text
    assert response.json()['applied'] == 'hot_reloaded'
    assert daemon._config_reload_trigger.is_set()
    assert yaml.safe_load(live.read_text())['poll_interval_seconds'] == 75
    assert yaml.safe_load(live.read_text())['github_token'] == '${GITHUB_TOKEN}'
    body['changes'] = {'poll_interval_seconds': 76}
    assert client.put('/api/config/section/global', json=body).status_code == 409
    config_seed.seed_config(seed, live)
    assert yaml.safe_load(live.read_text())['poll_interval_seconds'] == 75


def test_restart_required_save_keeps_existing_classification(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, live, client, _ = _dashboard(tmp_path, monkeypatch)
    response = client.put('/api/config/section/global', json={
        'store': 'config_yaml', 'section': 'global', 'base_hash': compute_content_hash(live),
        'changes': {'dashboard_port': 8099},
    })
    assert response.status_code == 200, response.text
    assert response.json()['applied'] == 'staged_restart'


def test_unrelated_symphony_save_keeps_other_symphony_secret_references(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, live, _, _ = _dashboard(tmp_path, monkeypatch)
    raw = yaml.safe_load(live.read_text())
    raw['symphonies'][1]['overrides'] = {'webhooks': {'secret': '${GITHUB_TOKEN}'}}
    live.write_text(yaml.safe_dump(raw))
    client, _ = _client(live, yaml.safe_load(expand_config_text(live.read_text())))
    before = client.get('/api/symphonies/demo')
    response = client.put('/api/symphonies/demo', headers={'If-Match': before.headers['ETag']},
                          json={'overrides': {'max_concurrent_cards': 5}})
    assert response.status_code == 200, response.text
    assert yaml.safe_load(live.read_text())['symphonies'][1]['overrides']['webhooks']['secret'] == '${GITHUB_TOKEN}'
    assert 'ghp_runtime_secret' not in live.read_text()


def test_helm_missing_persistence_values_default_on_for_old_releases() -> None:
    objects = _render(configPersistence=None)
    assert _container(objects)['args'][-1] == '/var/lib/coordinare/config/config.yaml'


def test_seed_hash_describes_the_bytes_copied_during_configmap_update(tmp_path: Path) -> None:
    seed, live = _paths(tmp_path)
    original_hash = compute_content_hash(seed)
    original_load = yaml.safe_load
    calls = []

    def load_then_project_new_config(document):
        parsed = original_load(document)
        if not calls:
            seed.write_text(SEED.replace('30', '90'))
            calls.append(True)
        return parsed

    with patch('coordinare.services.config_seed.yaml.safe_load', side_effect=load_then_project_new_config):
        config_seed.seed_config(seed, live)
    metadata = yaml.safe_load((live.parent / 'seed.yaml').read_text())
    assert metadata['seed_hash'] == original_hash
    assert config_seed.seed_status(seed, live)['chart_changed'] is True


@pytest.mark.parametrize('fingerprint', ['broken', '', '0' * 64])
def test_corrupt_section_fingerprints_report_unknown_provenance(tmp_path: Path, fingerprint: str) -> None:
    seed, live = _paths(tmp_path)
    config_seed.seed_config(seed, live)
    metadata_path = live.parent / 'seed.yaml'
    metadata = yaml.safe_load(metadata_path.read_text())
    metadata['sections']['github_org'] = fingerprint
    metadata_path.write_text(yaml.safe_dump(metadata))
    status = config_seed.seed_status(seed, live)
    assert status['chart_changed'] is None
    assert 'unknown' in status['message']
