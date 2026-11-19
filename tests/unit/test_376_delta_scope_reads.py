"""nare-native-017: a delta repair read one source outside the reviewed scope, reopened scope review, was rejected,
and the full repair it fell back to spent the rest of the attempt on four one-line command fixes."""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest


@pytest.mark.parametrize('delta', [True, False])
def test_only_a_delta_repair_refuses_a_source_outside_the_reviewed_scope(delta: bool) -> None:
    from coordinare.services.environment_construction import ReviewedConstruction

    scope = SimpleNamespace(all_inputs={'tests/test_sandbox.py': 'a' * 64})
    owner = SimpleNamespace(budget=SimpleNamespace(repair_delta=delta, scope_contract=scope))
    assert ReviewedConstruction.delta_scope_refusal(owner, 'tests/test_sandbox.py') is None  # type: ignore[arg-type]
    refusal = ReviewedConstruction.delta_scope_refusal(owner, 'tests/test_sandbox_live.py')  # type: ignore[arg-type]
    assert (refusal is not None and refusal['error'] == 'outside_reviewed_scope') is delta


@pytest.mark.asyncio
async def test_the_refused_read_is_an_answer_and_binds_nothing() -> None:
    from coordinare.services.environment_agent import PinnedSourceTools

    observed = []

    async def read_files(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError('a refused source must not be read')

    async def observe(inputs: dict[str, str]) -> None:
        observed.append(inputs)

    inspection = SimpleNamespace(policy_status=lambda path: 'readable', read_files=read_files, inputs={},
                                 scope_refusal=lambda path: {'path': path, 'error': 'outside_reviewed_scope'})
    tools = PinnedSourceTools(inspection, observe_sources=observe)  # type: ignore[arg-type]
    answer = json.loads(await tools.read({'path': 'tests/test_sandbox_live.py'}))
    assert answer == {'path': 'tests/test_sandbox_live.py', 'error': 'outside_reviewed_scope'}
    assert not observed and not inspection.inputs and tools.failure is None and not tools.issues()
