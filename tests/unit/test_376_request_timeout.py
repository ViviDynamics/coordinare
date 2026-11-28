"""nare-native-048: at medium effort 26 model calls outlasted the transport's 600 s read timeout and the run held on
provider_request_failed. The bootstrap's conducting configuration can set the per-request timeout."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from coordinare.config import ConductingConfig
from coordinare.services.agent_runtime.configuration import resolve_conducting


def _model(**fields):
    config = ConductingConfig(backend='openai_api', model='m', base_url='http://127.0.0.1:9/v1', api_key_env='K', **fields)
    return resolve_conducting(config, environ={'K': 'k'})


def test_the_configured_request_timeout_reaches_the_transport():
    model = _model(request_timeout=1800)
    assert model.request_timeout == 1800
    assert model.transport('system').timeout == 1800


def test_without_one_the_transport_keeps_its_default():
    assert _model().request_timeout is None
    assert _model().transport('system').timeout == 600


@pytest.mark.parametrize('value', [0, -1, 3601])
def test_an_out_of_range_timeout_is_refused(value):
    with pytest.raises(ValidationError):
        ConductingConfig(backend='openai_api', request_timeout=value)
