"""Encoding corrections apply only at a schema-known environment container path."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from coordinare.services.environment_proposal_patch import apply_patch


def test_encoded_plain_value_corrects_the_activation_environment_container():
    previous = {'RBENV_ROOT': '{cache}/rbenv', 'DOCKER_CONFIG': '{cache}/docker'}
    corrected = {'RBENV_ROOT': '{cache}/rbenv'}
    base = {'manifest': {'activation': {'env': previous}, 'checks': [{'id': 'untouched'}]}}
    original = copy.deepcopy(base)
    result = apply_patch(base, {'tool': 'repair_proposal', 'changes': [
        {'path': ['manifest', 'activation', 'env'], 'value': json.dumps(corrected)}]}, max_bytes=4096)
    assert result['manifest']['activation']['env'] == corrected
    assert result['manifest']['checks'] == original['manifest']['checks']
    assert base == original


@pytest.mark.parametrize('previous, replacement', [
    ('existing', '{"literal":"string"}'),
    ({}, '[1,2]'),
    ([], '{"wrong":"shape"}'),
    ({}, 'not JSON'),
    ({}, '{"duplicate":1,"duplicate":2}'),
    ({}, '{"nonfinite":NaN}'),
])
def test_plain_value_is_not_decoded_when_ambiguous_or_not_the_existing_collection_type(previous, replacement):
    result = apply_patch({'manifest': {'activation': {'env': previous}}}, {'tool': 'repair_proposal', 'changes': [
        {'path': ['manifest', 'activation', 'env'], 'value': replacement}]}, max_bytes=4096)
    assert result['manifest']['activation']['env'] == replacement


def test_invalid_previous_environment_value_does_not_change_a_valid_literal_string_correction():
    from coordinare_service_inference.environment.models import Activation

    literal = '{"enabled":true}'
    result = apply_patch({'manifest': {'activation': {'env': {'APP_JSON': {'enabled': True}}}}},
        {'tool': 'repair_proposal', 'changes': [
            {'path': ['manifest', 'activation', 'env', 'APP_JSON'], 'value': literal}]}, max_bytes=4096)
    activation = Activation.model_validate(result['manifest']['activation'])
    assert activation.env == {'APP_JSON': literal}


def test_an_arbitrary_previous_collection_does_not_change_a_literal_string_correction():
    result = apply_patch({'manifest': {'field': []}}, {'tool': 'repair_proposal', 'changes': [
        {'path': ['manifest', 'field'], 'value': '[1,2]'}]}, max_bytes=4096)
    assert result['manifest']['field'] == '[1,2]'


def test_encoded_environment_correction_still_passes_the_unchanged_global_admission():
    from pydantic import ValidationError

    from coordinare.services import environment_discovery as discovery
    from tests.unit.test_353_sharded_executable import global_part

    validator = discovery._CandidateValidator.__new__(discovery._CandidateValidator)
    validator.construction = SimpleNamespace(budget=SimpleNamespace(scope_contract=None),
        capabilities=SimpleNamespace(runtime=object(), platform='linux/aarch64'))
    validator.required_ids = {}
    admit = discovery._global_rules(validator)
    env = {'RBENV_ROOT': '{cache}/rbenv', 'DOCKER_CONFIG': '{cache}/docker'}
    base = {'manifest': global_part(activation={'env': env})}
    with pytest.raises(ValidationError):
        admit(base['manifest'])
    corrected = apply_patch(base, {'tool': 'repair_proposal', 'changes': [
        {'path': ['manifest', 'activation', 'env'], 'value': json.dumps({'RBENV_ROOT': env['RBENV_ROOT']})}]},
        max_bytes=4096)
    assert admit(corrected['manifest']) == corrected['manifest']
    assert corrected['manifest']['activation']['env'] == {'RBENV_ROOT': env['RBENV_ROOT']}
