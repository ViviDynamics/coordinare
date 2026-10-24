"""Repair guidance for the credential and envelope mistakes seen in the GLM Elm attempt."""
from __future__ import annotations

import json

import pytest
from coordinare_service_inference.environment.models import EnvironmentManifest
from pydantic import BaseModel, ConfigDict, ValidationError

from coordinare.services.environment_admission import (
    CREDENTIAL_GUIDANCE,
    NESTED_RESPONSES_GUIDANCE,
    _validation_feedback,
    nested_responses_guidance,
)
from coordinare.services.environment_discovery_prompts import (
    _MANIFEST_INSTRUCTIONS,
    LOCAL_CREDENTIAL_AUTHORING,
)
from tests.unit.test_353_local_credentials_contract import CHECK, CREDENTIAL, manifest


def feedback(data):
    with pytest.raises(ValidationError) as failure:
        EnvironmentManifest.model_validate(data)
    return _validation_feedback(failure.value)


def test_literal_database_password_guidance_points_to_local_credentials():
    check = {**CHECK, 'env': {'PGPASSWORD': 'postgres'}}
    [error] = feedback(manifest(checks=[check]))
    assert error['code'] == 'runtime_credential_reference' and error['guidance'] == CREDENTIAL_GUIDANCE
    assert 'local_credentials' in CREDENTIAL_GUIDANCE and 'scope text' in CREDENTIAL_GUIDANCE


def test_local_credential_codes_carry_their_own_guidance():
    [error] = feedback(manifest(local_credentials=[CREDENTIAL, {**CREDENTIAL, 'name': 'UNUSED'}]))
    assert error['code'] == 'local_credential_unused' and 'Reference every declared' in error['guidance']


def test_finding_responses_nested_in_scope_gets_envelope_guidance():
    class Scope(BaseModel):
        model_config = ConfigDict(extra='forbid')
        criteria: list[str]

    class Envelope(BaseModel):
        model_config = ConfigDict(extra='forbid')
        scope: Scope
        finding_responses: list[str]

    with pytest.raises(ValidationError) as failure:
        Envelope.model_validate({'scope': {'criteria': [], 'finding_responses': []}})
    errors = {tuple(e['location']): e for e in _validation_feedback(failure.value)}
    assert errors[('scope', 'finding_responses')]['guidance'] == NESTED_RESPONSES_GUIDANCE


def test_manifest_author_is_told_how_to_declare_local_credentials():
    assert LOCAL_CREDENTIAL_AUTHORING in ''.join(_MANIFEST_INSTRUCTIONS)


def test_missing_top_level_finding_responses_gets_envelope_guidance():
    class Envelope(BaseModel):
        model_config = ConfigDict(extra='forbid')
        scope: dict
        finding_responses: list[str]

    with pytest.raises(ValidationError) as failure:
        Envelope.model_validate({'scope': {}})
    [error] = _validation_feedback(failure.value)
    # Nothing was nested, so the envelope's main key is not known; the guidance never guesses one.
    assert error['location'] == ['finding_responses'] and error['guidance'] == nested_responses_guidance(None)


def test_responses_nested_in_a_revision_are_corrected_with_the_revision_key():
    class Revision(BaseModel):
        model_config = ConfigDict(extra='forbid')
        base_digest: str

    class Envelope(BaseModel):
        model_config = ConfigDict(extra='forbid')
        scope_revision: Revision
        finding_responses: list[str]

    with pytest.raises(ValidationError) as failure:
        Envelope.model_validate({'scope_revision': {'base_digest': 'x', 'finding_responses': []}})
    errors = _validation_feedback(failure.value)
    assert {error['guidance'] for error in errors} == {nested_responses_guidance('scope_revision')}
    assert '{"scope_revision": {...}, "finding_responses": [...]}' in errors[0]['guidance']


def test_bounded_errors_report_the_schema_bound_never_the_submitted_value():
    from typing import Literal

    from pydantic import Field

    class Envelope(BaseModel):
        model_config = ConfigDict(extra='forbid')
        verdict: Literal['accept', 'revise']
        rationale: str = Field(max_length=10)
        sources: list[str] = Field(min_length=1)

    with pytest.raises(ValidationError) as failure:
        Envelope.model_validate({'verdict': 'secret-verdict', 'rationale': 'secret-' * 5, 'sources': []})
    errors = {error['code']: error for error in _validation_feedback(failure.value)}
    assert errors['string_too_long']['max_length'] == 10
    assert errors['too_short']['min_length'] == 1
    assert "'accept'" in errors['literal_error']['allowed']
    assert 'secret' not in json.dumps(errors)


def test_missing_fields_ranges_patterns_and_shard_envelopes_are_named_from_the_schema():
    from pydantic import Field

    class Part(BaseModel):
        model_config = ConfigDict(extra='forbid')
        timeout_seconds: float = Field(gt=0, le=3600)
        id: str = Field(pattern=r'^[a-z]+$')
        checks: list[str]

    class Envelope(BaseModel):
        model_config = ConfigDict(extra='forbid')
        shard_checks: Part
        finding_responses: list[str]

    with pytest.raises(ValidationError) as failure:
        Envelope.model_validate({'shard_checks': {'timeout_seconds': 9999, 'id': 'SECRET!', 'finding_responses': []}})
    errors = _validation_feedback(failure.value)
    by_code = {error['code']: error for error in errors}
    assert by_code['less_than_equal']['at_most'] == 3600
    assert by_code['string_pattern_mismatch']['pattern'] == '^[a-z]+$'
    assert {error.get('missing_field') for error in errors if error['code'] == 'missing'} >= {'checks', 'finding_responses'}
    nested = [error for error in errors if error['code'] == 'extra_forbidden']
    assert nested and nested[0]['guidance'] == nested_responses_guidance('shard_checks')
    assert 'SECRET' not in json.dumps(errors)


def test_an_over_length_review_rationale_is_cut_with_a_mark_and_nothing_else_changes():
    from coordinare_service_inference.environment.scope import (
        bounded_rationale,
        normalize_source_assessment,
    )

    long = 'é' + 'evidence ' * 400
    data = {'scope_assessment': {'verdict': 'revise', 'rationale': long, 'sources': ['README.md'],
                                 'findings': [{'issue': 'x' * 3000}], 'decisions': []}}
    normalized = normalize_source_assessment(data)['scope_assessment']
    assert len(normalized['rationale'].encode()) <= 2048 and normalized['rationale'].endswith(' [truncated]')
    assert normalized['findings'] == data['scope_assessment']['findings'] and normalized['verdict'] == 'revise'
    assert bounded_rationale('short') == 'short'


def test_an_incomplete_criterion_mapping_names_the_criteria_never_the_authors_keys():
    from pydantic_core import PydanticCustomError

    class Envelope(BaseModel):
        model_config = ConfigDict(extra='forbid')
        criterion_checks: dict

        @classmethod
        def check(cls, data):
            raise PydanticCustomError('scope_required_check_mapping', 'mapping',
                {'missing_criteria': ['S1111111111111111'], 'invalid_criteria': ['S2222222222222222'], 'unexpected_keys': 1})

    from pydantic import model_validator

    class Mapped(Envelope):
        @model_validator(mode='after')
        def mapped(self):
            Envelope.check(None)

    with pytest.raises(ValidationError) as failure:
        Mapped.model_validate({'criterion_checks': {'author-invented-key': ['x']}})
    [error] = _validation_feedback(failure.value)
    assert error['code'] == 'scope_required_check_mapping'
    assert error['missing_criteria'] == ['S1111111111111111'] and error['invalid_criteria'] == ['S2222222222222222']
    assert error['unexpected_keys'] == 1 and 'author-invented-key' not in json.dumps(error)


def test_local_credential_errors_name_the_credentials_and_command_they_reject():
    """v34: told neither the credential nor the command, the author repeated each mistake."""
    [error] = feedback(manifest(local_credentials=[CREDENTIAL, {**CREDENTIAL, 'name': 'UNUSED'}]))
    assert error['unavailable'] == ['UNUSED']
    [error] = feedback(manifest(local_credentials=[{**CREDENTIAL, 'service': 'missing'}]))
    assert (error['code'], error['unavailable']) == ('local_credential_invalid', ['PGPASSWORD'])
    install = {'id': 'seed-db', 'argv': ['sh', '-c', 'echo ${PGPASSWORD}']}
    [error] = feedback(manifest(install=[install]))
    assert (error['code'], error['command_id'], error['unavailable']) == (
        'local_credential_in_install', 'seed-db', ['PGPASSWORD'])
