"""A repair built from an accepted manifest keeps the forwards that manifest declared.

Dettmore e2e-65: a delta repair's base had no runtime_endpoints (a manifest keeps them under runtime.endpoints), so the
repaired manifest lost the postgres forward and the next execution's carrier-side health probe found nothing listening.
"""
from __future__ import annotations

from coordinare_service_inference.environment.runtime import (
    RuntimeEndpoint,
    RuntimePolicy,
    RuntimeSpec,
)

from coordinare.services.environment_delta import proposal_of
from tests.unit.test_353_execution_obligations import contract
from tests.unit.test_353_runtime_identity import policy_data


def test_a_repair_base_keeps_the_declared_forwards():
    endpoint = RuntimeEndpoint(id='postgres', bind_address='127.0.0.1', guest_port=5432, outer_port=5432)
    runtime = RuntimeSpec(schema_version=1, policy=RuntimePolicy.model_validate(policy_data()), endpoints=[endpoint])
    manifest = contract().model_copy(update={'runtime': runtime})
    assert proposal_of(manifest)['runtime_endpoints'] == [endpoint.model_dump(mode='json')]


def test_a_manifest_without_a_private_runtime_has_no_forwards():
    assert proposal_of(contract())['runtime_endpoints'] == []


def test_the_attempt_summary_names_why_a_delta_repair_fell_back():
    from coordinare.services.environment_attempt import AttemptBudget

    budget = AttemptBudget()
    assert budget.summary()['delta_fallback_code'] is None
    budget.delta_fallback_code = 'repair_delta_rejected'
    assert budget.summary()['delta_fallback_code'] == 'repair_delta_rejected'
