"""Bootstrap binds the configured route without silently changing providers."""
from __future__ import annotations

import pytest

from coordinare.config import ConductingConfig
from coordinare.services.environment_attempt import EnvironmentDiscoveryError


def test_unsupported_endpoint_fails_before_request() -> None:
    from coordinare.services.agent_runtime.configuration import resolve_conducting

    with pytest.raises(EnvironmentDiscoveryError, match='API transport') as caught:
        resolve_conducting(ConductingConfig(backend='claude_cli'), environ={})
    assert caught.value.code == 'agent_transport_unsupported'


def test_selected_model_parameters_are_preserved() -> None:
    from coordinare.services.agent_runtime.configuration import resolve_conducting

    bound = resolve_conducting(ConductingConfig(backend='openai_api', model='custom/model',
        base_url='https://model.example/v1', api_key_env='SELECTED_KEY', max_tokens=32768,
        temperature=0.3, effort='high'), environ={'SELECTED_KEY': 'fixture-secret', 'OPENAI_API_KEY': 'wrong-key'})
    assert bound.provider == 'openai'
    assert bound.model == 'custom/model'
    assert bound.base_url == 'https://model.example/v1'
    assert bound.api_key == 'fixture-secret'
    assert bound.max_tokens == 32768
    assert bound.temperature == 0.3
    assert bound.effort == 'high'
    assert 'fixture-secret' not in repr(bound)
    assert 'wrong-key' not in repr(bound)


def test_anthropic_does_not_inherit_openai_credentials() -> None:
    from coordinare.services.agent_runtime.configuration import resolve_conducting

    bound = resolve_conducting(ConductingConfig(backend='anthropic_api', model='chosen-anthropic', effort=None),
                              environ={'ANTHROPIC_API_KEY': 'anthropic-fixture', 'OPENAI_API_KEY': 'wrong-key'})
    assert bound.provider == 'anthropic'
    assert bound.model == 'chosen-anthropic'
    assert bound.api_key == 'anthropic-fixture'


def test_missing_selected_credential_cannot_fall_back_to_another_route() -> None:
    from coordinare.services.agent_runtime.configuration import resolve_conducting

    with pytest.raises(EnvironmentDiscoveryError) as caught:
        resolve_conducting(ConductingConfig(backend='openai_api', api_key_env='SELECTED_KEY'),
                          environ={'OPENAI_API_KEY': 'wrong-key'})
    assert caught.value.code == 'agent_credentials_missing'


def test_unsupported_anthropic_sampling_fails_at_configuration() -> None:
    from coordinare.services.agent_runtime.configuration import resolve_conducting

    with pytest.raises(EnvironmentDiscoveryError) as caught:
        resolve_conducting(ConductingConfig(backend='anthropic_api', temperature=0.4),
                          environ={'ANTHROPIC_API_KEY': 'fixture'})
    assert caught.value.code == 'agent_parameters_unsupported'
