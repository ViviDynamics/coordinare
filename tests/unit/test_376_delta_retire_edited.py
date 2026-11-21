"""nare-native-024: a delta author rewrote a failed check in place ten times, though each correction said to retire it.

Five checks had failed because they were wrong. Retiring one means keeping it exactly as it ran, no longer required,
and adding a new required check for its obligations; the author kept editing the check itself until the attempt ran
out of schema corrections. A rewrite of a check that failed and never passed can only be that retirement, so the
daemon makes it: the executed record stays, retired, and the rewrite becomes a new required check that takes over its
mappings and dependents. The independent review still judges the replacement.
"""
from __future__ import annotations

import copy
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


@pytest.mark.asyncio
async def test_a_rewrite_the_author_also_added_under_its_own_id_reuses_that_replacement() -> None:
    """nare-native-026: the author rewrote the check in place and added the same command as its own replacement; a
    second copy under a derived id was refused as duplicate content until the corrections ran out."""
    from tests.unit.test_353_discovery_repair import assessment, candidate

    async def chat(messages: Any) -> str:
        context = json.loads(messages[1]["content"])
        if "manifest" in context:
            return json.dumps(assessment(candidate()))
        mapping = {criterion["id"]: ["version"] for criterion in context["scope_contract"]["definition"]["criteria"]}
        fixed = ["zog", "--version", "--quiet"]
        return json.dumps({"manifest_delta": {"criterion_checks": mapping, "checks": [
            {"id": "version", "argv": fixed, "required": False},
            {"id": "version-quiet", "argv": fixed, "required": False}]}, "finding_responses": []})

    budget = AttemptBudget()
    budget.retirable_checks = frozenset({"version"})
    manifest, _review = await delta_run(chat, budget=budget, executed=True)

    assert budget.schema_repairs == 0 and budget.delta_fallback is False
    checks = {check.id: check for check in manifest.checks}
    assert "version-r1" not in checks and checks["version"].required is False
    assert checks["version-quiet"].required is True
    assert all("version-quiet" in ids and "version" not in ids for ids in manifest.criterion_checks.values())


def test_the_global_part_keeps_a_retired_check_without_counting_it_removed() -> None:
    """nare-native-026: the divided build's global rules counted a retired, still declared check as a removed
    required record, though the candidate validator's lock keeps it; every global retirement was refused."""
    from types import SimpleNamespace

    from pydantic import ValidationError

    from coordinare.services import environment_discovery as discovery_module
    from coordinare.services.environment_admission import _validation_feedback
    from tests.unit.test_353_sharded_executable import global_part, make_criterion, scope_of

    merge = discovery_module._CandidateValidator.__new__(discovery_module._CandidateValidator)
    merge.construction = SimpleNamespace(budget=SimpleNamespace(scope_contract=scope_of([make_criterion(1)])),
                                         capabilities=SimpleNamespace(runtime=None, platform='linux/aarch64'))
    merge.required_ids = {'requirements': set(), 'checks': {'toolchain'}, 'install': set(), 'services': set()}
    retired = global_part(checks=[{'id': 'toolchain', 'argv': ['true'], 'required': False},
                                  {'id': 'toolchain-r1', 'argv': ['true', 'fixed']}])
    merge.budget = SimpleNamespace(executed_manifests=[], retirable_checks=frozenset({'toolchain'}))
    discovery_module._global_rules(merge)(copy.deepcopy(retired))
    # A check that may not be retired is still a removed obligation when it stops being required.
    merge.budget = SimpleNamespace(executed_manifests=[], retirable_checks=frozenset())
    with pytest.raises(ValidationError) as caught:
        discovery_module._global_rules(merge)(copy.deepcopy(retired))
    assert {item['code'] for item in _validation_feedback(caught.value)} == {'global_obligation_removed'}
