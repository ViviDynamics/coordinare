"""A held discovery names the exception behind invalid_contract without echoing model text (353 repair diagnostics)."""
from __future__ import annotations

import pytest

from coordinare.services import environment_discovery as discovery_module
from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError


def _raise_inside_discovery(text: str) -> None:
    raise TypeError(text)


@pytest.mark.asyncio
async def test_an_invalid_contract_hold_names_the_exception_and_frame_but_not_its_message(monkeypatch):
    async def attempt(**_kwargs):
        _raise_inside_discovery('model said: rm -rf /secret')

    monkeypatch.setattr(discovery_module, '_discover_attempt', attempt)
    budget = AttemptBudget()
    with pytest.raises(EnvironmentDiscoveryError) as held:
        await discovery_module.discover_and_review(
            symphony_name='dettmore-platform', commit='a' * 40, paths=['README.md'], fetch=None,
            chat=lambda *_a, **_k: None, budget=budget, capabilities=object(), require_preflight=False)

    assert held.value.code == 'invalid_contract'
    assert budget.error_code == 'invalid_contract'
    assert budget.error_detail is not None
    assert 'TypeError' in budget.error_detail
    assert '_raise_inside_discovery' in budget.error_detail
    assert 'secret' not in budget.error_detail and 'rm -rf' not in str(held.value)
    assert budget.summary()['error_detail'] == budget.error_detail
