"""163: retain the measured harmful case from research R2."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from coordinare.config import ModelEndpoint


@pytest.mark.parametrize('model', ['glm-5.3-flash', 'test-vendor/glm-5.3-flash', 'TEST-VENDOR/GLM-5.3-FLASH'])
def test_measured_harmful_model_cannot_opt_in(model):
    with pytest.raises(ValidationError, match='measured harmful'):
        ModelEndpoint(name='m', endpoint='e', model=model, reasoning_policy='disable_thinking')
    assert ModelEndpoint(name='m', endpoint='e', model=model).reasoning_policy is None


@pytest.mark.parametrize('model', ['ada/qwen3-14b', 'ada/qwen3-8b', 'unmeasured-model'])
def test_policy_is_always_explicit_even_for_beneficial_or_unknown_models(model):
    assert ModelEndpoint(name='m', endpoint='e', model=model).reasoning_policy is None


def test_evidence_distinguishes_harmful_beneficial_and_unmeasured():
    import json
    from pathlib import Path

    evidence = json.loads((Path(__file__).parents[2] / "specs/163-reasoning-policy/policy-evidence.json").read_text())
    glm_observations = [value for model, value in evidence.items() if model.rsplit("/", 1)[-1] == "glm-5.3-flash"]
    assert len(glm_observations) == 1
    assert glm_observations[0]["status"] == "measured_harmful"
    assert evidence["ada/qwen3-14b"]["status"] == "measured_beneficial"
    assert evidence["default"]["status"] == "not_measured"
