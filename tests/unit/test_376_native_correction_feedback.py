"""Native corrections must deliver the domain's actionable, sanitized details."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest
from coordinare_service_inference.environment.models import EnvironmentManifest

from coordinare.services.agent_runtime.nare_runtime import NareRuntime
from coordinare.services.environment_admission import _validation_feedback
from coordinare.services.environment_agent import EnvironmentPhaseRequest, run_environment_phase
from coordinare.services.environment_attempt import AttemptBudget
from tests.unit.test_353_scope_mapping import mapped_manifest
from tests.unit.test_376_bootstrap_phases import PhaseTransport


class CorrectionTransport(PhaseTransport):
    def __init__(self, bad: dict[str, Any], good: dict[str, Any]) -> None:
        super().__init__({'manifest': bad})
        self.good = good
        self.proposed = False
        self.feedback = ''

    async def turn(self, messages: Any, tools: Any) -> Any:
        if self.proposed:
            self.feedback = '\n'.join(block.get('text', '') for block in messages[-1].content)
            self.answer = {'manifest': self.good}
        reply = await super().turn(messages, tools)
        self.proposed |= reply.stop_reason == 'end_turn'
        return reply


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['missing', 'invalid', 'collision'])
async def test_domain_error_details_reach_the_next_native_model_request(mutation: str) -> None:
    good, criterion_id = await mapped_manifest()
    bad = deepcopy(good)
    if mutation == 'missing':
        bad['criterion_checks'] = {}
    elif mutation == 'invalid':
        bad['criterion_checks'][criterion_id] = ['absent-check']
    else:
        bad['requirements'][0]['id'] = 'operational'
    transport = CorrectionTransport(bad, good)
    budget = AttemptBudget(max_schema_repairs=1)
    request = EnvironmentPhaseRequest(
        attempt_id='correction', phase='discovery', role='author', source_commit=good['source_commit'],
        domain_digests=(), model_config_digest='a' * 64, instructions='Declare the manifest.',
        packet={}, sources=(), required=frozenset(), envelope='manifest',
        schema={'type': 'object', 'properties': {'manifest': {'type': 'object'}}, 'required': ['manifest']},
        validate=EnvironmentManifest.model_validate, rejection_errors=_validation_feedback,
        runtime=NareRuntime(transport=transport), budget=budget, phase_timeout_seconds=10,
    )
    result = await run_environment_phase(request)
    assert result.value == EnvironmentManifest.model_validate(good)
    assert budget.schema_repairs == 1
    assert budget.calls == budget.calls_completed == 3
    if mutation == 'collision':
        assert 'record_id_collision' in transport.feedback
        assert json.dumps(['operational']) in transport.feedback
        assert 'unavailable' in transport.feedback
    else:
        assert 'scope_required_check_mapping' in transport.feedback
        assert criterion_id in transport.feedback
        assert ('missing_criteria' if mutation == 'missing' else 'invalid_criteria') in transport.feedback
    assert 'absent-check' not in transport.feedback  # Arbitrary candidate values stay excluded.
