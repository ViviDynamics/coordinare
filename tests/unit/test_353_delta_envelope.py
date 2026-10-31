"""A delta repair conversation keeps asking for a delta, and a delta can declare a generated credential.

Dettmore e2e-05: nine checks used an undeclared ${SECRET_KEY_BASE}. The delta's global author correctly declared it as
a local credential, but the delta schema had no local_credentials field. Every later correction then asked for a
complete manifest envelope (the stage prompts reset the expected envelope and offered the full schema and patches),
while the parser still accepted only manifest_delta, so each conforming reply was unsupported. The delta spent seven of
the attempt's eight schema repairs, and the full repair that followed lost the last one at once.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from coordinare.services.environment_delta_schema import MANIFEST_DELTA_ENVELOPE, apply_delta
from coordinare.services.environment_discovery_prompts import DiscoveryPrompts


def _credential(name, service='db'):
    return {'name': name, 'service': service, 'purpose': 'generated for the test database'}


def test_a_delta_may_add_a_local_credential_and_replace_one_by_name():
    base = {'install': [], 'checks': [], 'local_credentials': [_credential('DATABASE_PASSWORD')]}
    delta = {'local_credentials': [_credential('SECRET_KEY_BASE', 'app'), _credential('DATABASE_PASSWORD', 'pg')]}
    MANIFEST_DELTA_ENVELOPE.model_validate({'manifest_delta': delta, 'finding_responses': []})
    applied = apply_delta(base, delta)
    assert [(item['name'], item['service']) for item in applied['local_credentials']] == [
        ('DATABASE_PASSWORD', 'pg'), ('SECRET_KEY_BASE', 'app')]
    assert base['local_credentials'][0]['service'] == 'db'


def _budget():
    return SimpleNamespace(expected_envelope='manifest_delta', probe_reports=[{'id': 'p'}], finding_ledger=None)


def test_the_construction_prompt_keeps_a_delta_conversation_on_the_delta_envelope():
    messages = [{'role': 'system', 'content': ''}, {'role': 'user', 'content': '{}'}]
    budget = _budget()
    DiscoveryPrompts().update(messages, budget, {'Gemfile': 'a' * 64}, repairing=True, delta=True)
    assert budget.expected_envelope == 'manifest_delta'
    system = messages[0]['content']
    assert 'manifest_delta' in system and 'repair_proposal' not in system
    assert json.dumps(MANIFEST_DELTA_ENVELOPE.model_json_schema()) in system


def test_a_full_construction_prompt_is_unchanged():
    messages = [{'role': 'system', 'content': ''}, {'role': 'user', 'content': '{}'}]
    budget = _budget()
    DiscoveryPrompts().update(messages, budget, {'Gemfile': 'a' * 64}, repairing=True)
    assert budget.expected_envelope == 'manifest'
    assert 'repair_proposal' in messages[0]['content']


def test_a_delta_phase_offers_the_delta_schema_and_no_patches():
    from coordinare.services import environment_discovery as discovery_module

    budget = SimpleNamespace(expected_envelope='manifest_delta', scope_contract=None, finding_ledger=None)
    inspection = SimpleNamespace(budget=budget, delta=True, inputs={})
    schema = discovery_module._phase_schema(inspection, {'manifest_delta': {}, 'finding_responses': []})
    assert 'repair_proposal' not in json.dumps(schema)
    assert 'manifest_delta' in json.dumps(schema)


def test_a_duplicate_credential_name_in_a_delta_stays_visible_to_admission():
    base = {'local_credentials': [_credential('PGPASSWORD')]}
    delta = {'local_credentials': [_credential('PGPASSWORD', 'a'), _credential('PGPASSWORD', 'b')]}
    applied = apply_delta(base, delta)
    assert [(item['name'], item['service']) for item in applied['local_credentials']] == [
        ('PGPASSWORD', 'a'), ('PGPASSWORD', 'b')]


def test_a_probe_correction_within_a_delta_repair_may_still_be_patched():
    from coordinare.services import environment_discovery as discovery_module

    budget = SimpleNamespace(expected_envelope='probe', scope_contract=None, finding_ledger=None)
    inspection = SimpleNamespace(budget=budget, delta=True, inputs={'Gemfile': 'a' * 64})
    base = {'probe': {'execution_context': 'outer', 'commands': [{'id': 'p', 'argv': ['true']}],
                      'citations': {'p': ['Gemfile']}, 'questions': {'p': 'q'}}}
    assert 'repair_proposal' in json.dumps(discovery_module._phase_schema(inspection, base))
