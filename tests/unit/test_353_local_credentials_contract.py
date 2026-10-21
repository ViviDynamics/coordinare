"""Credentials for services the manifest itself starts are declared by name and never written as literals."""
from __future__ import annotations

import pytest
from coordinare_service_inference.environment.models import EnvironmentManifest
from pydantic import ValidationError

from tests.unit.env_manifest_fixture import reviewed_metadata

POSTGRES = {'id': 'postgres',
            'start': {'id': 'postgres-start', 'argv': ['pg_ctl', 'start', '-D', '{scratch}/pg']},
            'health': {'id': 'postgres-health', 'argv': ['pg_isready']},
            'stop': {'id': 'postgres-stop', 'argv': ['pg_ctl', 'stop', '-D', '{scratch}/pg']},
            'init': {'id': 'postgres-init', 'argv': ['sh', '-c', 'initdb --pwfile=<(printf %s "$PGPASSWORD") {scratch}/pg'],
                     'env': {'PGPASSWORD': '${PGPASSWORD}'}}}
CHECK = {'id': 'finance-suites', 'argv': ['bash', 'scripts/ci/run_finance_postgres_suites.sh'],
         'env': {'TEST_DATABASE_URL': 'postgres://postgres:${PGPASSWORD}@localhost:5432/elm'},
         'depends_on': ['postgres-health']}
CREDENTIAL = {'name': 'PGPASSWORD', 'service': 'postgres', 'purpose': 'PostgreSQL superuser password for the test database'}


def manifest(**changes):
    data = reviewed_metadata(checks=[CHECK], services=[POSTGRES])['environment_manifest']
    data['local_credentials'] = [CREDENTIAL]
    data.update(changes)
    return data


def codes(data):
    with pytest.raises(ValidationError) as failure:
        EnvironmentManifest.model_validate(data)
    return [error['type'] for error in failure.value.errors()]


def test_elm_shaped_manifest_with_a_generated_database_password_validates():
    validated = EnvironmentManifest.model_validate(manifest())
    assert [credential.name for credential in validated.local_credentials] == ['PGPASSWORD']
    assert validated.model_dump()['local_credentials'][0]['service'] == 'postgres'


def test_without_local_credentials_the_digest_is_unchanged():
    data = reviewed_metadata()
    plain = EnvironmentManifest.model_validate(data['environment_manifest'])
    assert 'local_credentials' not in plain.model_dump()
    assert plain.digest() == data['environment_manifest_digest']


def test_literal_passwords_are_still_rejected():
    check = {**CHECK, 'env': {'TEST_DATABASE_URL': 'postgres://postgres:postgres@localhost:5432/elm'}}
    assert 'runtime_credential_reference' in codes(manifest(checks=[check]))


@pytest.mark.parametrize('credentials', [
    [CREDENTIAL, CREDENTIAL],
    [{**CREDENTIAL, 'service': 'redis'}],
])
def test_duplicate_or_unbound_credentials_are_invalid(credentials):
    assert 'local_credential_invalid' in codes(manifest(local_credentials=credentials))


@pytest.mark.parametrize('name', ['COORDINARE_ENV_X', 'PATH', 'BASH_ENV', 'ENV', 'COORDINARE_REPO_DIR', '1BAD'])
def test_reserved_or_malformed_names_are_rejected(name):
    assert codes(manifest(local_credentials=[{**CREDENTIAL, 'name': name}]))


def test_unreferenced_credential_is_rejected():
    extra = {**CREDENTIAL, 'name': 'UNUSED_PASSWORD'}
    assert 'local_credential_unused' in codes(manifest(local_credentials=[CREDENTIAL, extra]))


def test_install_commands_cannot_use_generated_credentials():
    install = [{'id': 'seed', 'argv': ['sh', '-c', 'echo ${PGPASSWORD}'], 'env': {'PGPASSWORD': '${PGPASSWORD}'}}]
    assert 'local_credential_in_install' in codes(manifest(install=install))
