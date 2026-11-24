"""nare-native-036: the global delta author retired check-replay and check-pytest-sample by rewriting them (renamed to
-r1 replacements). The c00- and c01- criteria still mapped the old ids; the global author may not write mappings, c01-
was carried unchanged and c00-'s stale part was replayed with no model call, and the error held the attempt with 5.8
hours left. Each of those is fixed here."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from coordinare_service_inference.environment.models import ShardChecks

from coordinare.services.environment_delta import (
    DELTA_FALLBACK,
    DeltaPlan,
    renamed_part,
    retirement_renames,
)
from coordinare.services.environment_scope import _ResponseReferenceError
from coordinare.services.environment_shard_construction import (
    CheckGroup,
    Inheritance,
    validate_group,
)


def cmd(identifier: str, argv: list[str], **extra: Any) -> dict[str, Any]:
    return {'id': identifier, 'argv': argv, **extra}


BASE = {'checks': [cmd('check-replay', ['nare', 'run']), cmd('check-lint', ['ruff', 'check'])]}
RETIRED = {'checks': [cmd('check-replay', ['nare', 'run'], required=False),
                      cmd('check-replay-r1', ['nare', 'run', '--yes', 'task']), cmd('check-lint', ['ruff', 'check'])]}


def test_a_retired_global_check_is_renamed_to_its_replacement() -> None:
    assert retirement_renames(BASE, RETIRED) == {'check-replay': 'check-replay-r1'}
    # An unchanged or already-retired check, or one retired without a replacement, is not renamed.
    assert retirement_renames(BASE, BASE) == {}
    assert retirement_renames(RETIRED, RETIRED) == {}
    assert retirement_renames(BASE, {'checks': [cmd('check-replay', ['nare', 'run'], required=False)]}) == {}


def test_every_group_part_maps_and_depends_on_the_replacement() -> None:
    part = {'checks': [cmd('c01-x', ['x'], depends_on=['check-replay']), cmd('c01-y', ['y'])],
            'criterion_checks': {'S1': ['check-replay', 'c01-x'], 'S2': ['check-replay-r1', 'check-replay']}}
    renamed = renamed_part(part, {'check-replay': 'check-replay-r1'})
    assert renamed['criterion_checks'] == {'S1': ['check-replay-r1', 'c01-x'], 'S2': ['check-replay-r1']}
    assert renamed['checks'][0]['depends_on'] == ['check-replay-r1']
    assert renamed_part(part, {}) == part
    ShardChecks.model_validate(renamed)


def test_a_delta_that_still_cannot_map_falls_back_to_the_full_repair() -> None:
    assert {'criterion_mapping_invalid', 'inherited_mapping_missing'} <= DELTA_FALLBACK


def test_a_retirement_that_also_changed_depends_on_keeps_the_executed_dependencies_and_mints_nothing() -> None:
    ran = cmd('c00-evidence', ['regen'], depends_on=['c00-version'], timeout_seconds=600)
    resent = {**ran, 'required': False, 'depends_on': []}
    plan = DeltaPlan(base=None, triage=SimpleNamespace(closure_ids=['c00-evidence']),  # type: ignore[arg-type]
                     fixed=frozenset({'c00-evidence'}), retirable=frozenset({'c00-evidence'}))
    [kept] = plan.retire_edited({'checks': [ran]}, {'checks': [resent]})['checks']
    assert kept == {**resent, 'depends_on': ['c00-version']}


def test_the_mapping_correction_names_the_unusable_checks() -> None:
    group = cast('CheckGroup', SimpleNamespace(prefix='c00-', ids={'S1'}))
    summary = {'checks': [{'id': 'check-replay', 'required': False}, {'id': 'check-replay-r1'}], 'readiness_ids': []}
    part = {'shard_checks': {'checks': [], 'criterion_checks': {'S1': ['check-replay']}}}
    with pytest.raises(_ResponseReferenceError) as refused:
        validate_group(part, group=group, summary=summary, inherited=Inheritance())
    [error] = [error for error in refused.value.errors if error['code'] == 'criterion_mapping_invalid']
    assert error['unusable'] == ['check-replay'] and 'map its required replacement' in error['guidance']
    validate_group({'shard_checks': {'checks': [], 'criterion_checks': {'S1': ['check-replay-r1']}}}, group=group,
                   summary=summary, inherited=Inheritance())



@pytest.mark.asyncio
async def test_a_delta_round_renames_a_retired_global_check_in_carried_and_authored_groups(monkeypatch) -> None:
    from coordinare.services import environment_discovery as discovery_module

    c00, c01 = CheckGroup('c00-', ()), CheckGroup('c01-', ())
    carried = ShardChecks.model_validate({'checks': [], 'criterion_checks': {'S9': ['check-replay']}})
    authored = ShardChecks.model_validate({'checks': [cmd('c00-x', ['x'], depends_on=['check-replay'])],
                                           'criterion_checks': {'S1': ['check-replay', 'c00-x']}})

    async def global_and_groups(*_args: Any) -> Any:
        return RETIRED, {'c00-': (c00, authored)}

    monkeypatch.setattr(discovery_module, '_global_and_groups', global_and_groups)
    monkeypatch.setattr(discovery_module, '_carried_parts', lambda _plan, _groups: {'c01-': (c01, carried)})
    plan = SimpleNamespace(group_touched=lambda group: group.prefix == 'c00-', base_global=lambda _groups: BASE)
    build = SimpleNamespace(groups=[c00, c01], revising=False)
    attempt = SimpleNamespace(delta=plan, budget=SimpleNamespace(probe_reports=[]))
    monkeypatch.setattr(discovery_module, '_speculative_base', lambda *_args: None)
    _global, parts = await discovery_module._author_round(build, attempt, lambda data: data)  # type: ignore[arg-type]
    assert parts['c01-'][1].criterion_checks == {'S9': ['check-replay-r1']}
    assert parts['c00-'][1].criterion_checks == {'S1': ['check-replay-r1', 'c00-x']}
    assert parts['c00-'][1].checks[0].depends_on == ['check-replay-r1']
