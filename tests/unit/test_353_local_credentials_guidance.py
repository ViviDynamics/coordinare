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
