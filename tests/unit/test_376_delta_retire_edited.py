"""nare-native-024: a delta author rewrote a failed check in place ten times, though each correction said to retire it.

Five checks had failed because they were wrong. Retiring one means keeping it exactly as it ran, no longer required,
and adding a new required check for its obligations; the author kept editing the check itself until the attempt ran
out of schema corrections. A rewrite of a check that failed and never passed can only be that retirement, so the
daemon makes it: the executed record stays, retired, and the rewrite becomes a new required check that takes over its
mappings and dependents. The independent review still judges the replacement.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from coordinare.services.environment_attempt import AttemptBudget
from tests.unit.test_353_delta_repair import delta_run


def _rewrite_in_place(*, required: bool) -> tuple[list[str], Any]:
    from tests.unit.test_353_discovery_repair import assessment, candidate

    kinds: list[str] = []

    async def chat(messages: Any) -> str:
        context = json.loads(messages[1]["content"])
        if "manifest" in context:
            return json.dumps(assessment(candidate()))
        kinds.append("delta" if "repair_delta" in context else "full")
        mapping = {criterion["id"]: ["version"] for criterion in context["scope_contract"]["definition"]["criteria"]}
        return json.dumps({"manifest_delta": {"criterion_checks": mapping, "checks": [
            {"id": "version", "argv": ["zog", "--version", "--quiet"], "required": required}]}, "finding_responses": []})

    return kinds, chat


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [False, True], ids=["marked-retired", "kept-required"])
async def test_a_failed_check_rewritten_in_place_is_retired_and_replaced(required: bool) -> None:
    kinds, chat = _rewrite_in_place(required=required)
    budget = AttemptBudget()
    budget.retirable_checks = frozenset({"version"})
    manifest, _review = await delta_run(chat, budget=budget, executed=True)

    assert kinds == ["delta"] and budget.schema_repairs == 0 and budget.delta_fallback is False
    checks = {check.id: check for check in manifest.checks}
    previous = checks["version"]
    assert previous.required is False and previous.argv != ["zog", "--version", "--quiet"]
    replacement = checks["version-r1"]
    assert replacement.required is True and replacement.argv == ["zog", "--version", "--quiet"]
    assert all("version" not in ids and "version-r1" in ids for ids in manifest.criterion_checks.values())
    assert all("version" not in requirement.checks for requirement in manifest.requirements)


@pytest.mark.asyncio
async def test_a_check_that_may_not_be_retired_is_not_converted() -> None:
    """Without a failed execution the check is not retirable: its rewrite is still refused, never converted."""
    from coordinare.services.environment_attempt import EnvironmentDiscoveryError

    kinds, chat = _rewrite_in_place(required=True)
    budget = AttemptBudget()
    with pytest.raises(EnvironmentDiscoveryError) as held:
        await delta_run(chat, budget=budget, executed=True)
    assert held.value.code == "attempt_schema_limit"
    assert kinds[0] == "delta" and budget.schema_repairs == budget.max_schema_repairs
