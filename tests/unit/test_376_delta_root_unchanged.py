"""nare-native-029: the delta failed install-aws and install-bench-image. Both delta rounds changed install-aws and left
install-bench-image exactly as it ran, and review spent a round each time saying so before the full repair took over.
The daemon now refuses a failed command left as it ran in the delta author's own phase, as a correction naming the id:
an install unless what it depends on changed, a check or service command unless anything it may rely on changed."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_delta import DeltaPlan
from coordinare.services.environment_obligations import REPAIR_CORRECTABLE, REPAIR_ROOT_UNCHANGED


def cmd(identifier: str, argv: list[str], **extra: Any) -> dict[str, Any]:
    return {'id': identifier, 'argv': argv, **extra}


BASE = {'requirements': [], 'services': [], 'checks': [], 'activation': {'path': [], 'env': {}},
        'install': [cmd('install-uv', ['pip', 'install', 'uv']),
                    cmd('install-aws', ['unzip-and-install']),
                    cmd('install-bench-image', ['docker', 'build', 'benchmarks']),
                    cmd('install-sync', ['uv', 'sync'], depends_on=['install-uv'])]}
PLAN = DeltaPlan(base=None,  # type: ignore[arg-type]  # guard_part reads only the triage.
                 triage=SimpleNamespace(roots={'install-aws': 'command', 'install-bench-image': 'command'},  # type: ignore[arg-type]
                                        closure_ids=['install-aws', 'install-bench-image', 'install-sync'],
                                        touched_parts=['global']))


def candidate(*changes: dict[str, Any], activation: dict[str, Any] | None = None) -> dict[str, Any]:
    changed = {item['id']: item for item in changes}
    install = [changed.pop(item['id'], item) for item in BASE['install']]
    return {**BASE, 'install': [*install, *changed.values()], **({'activation': activation} if activation else {})}


def test_a_delta_that_repairs_one_failed_install_and_leaves_the_other_as_it_ran_is_corrected() -> None:
    with pytest.raises(EnvironmentDiscoveryError) as raised:
        PLAN.guard_part(BASE, candidate(cmd('install-aws', ['unzip-and-install', '--chmod'])))
    assert raised.value.code == REPAIR_ROOT_UNCHANGED in REPAIR_CORRECTABLE
    assert 'install-bench-image' in str(raised.value) and 'install-aws' not in str(raised.value)


def test_a_delta_that_changes_every_failed_install_passes() -> None:
    PLAN.guard_part(BASE, candidate(cmd('install-aws', ['unzip-and-install', '--chmod']),
                                    cmd('install-bench-image', ['docker', 'build', '-f', 'benchmarks/Dockerfile', '.'])))


@pytest.mark.parametrize('change', [
    # What it depends on changed.
    {'install': [cmd('install-uv', ['pip', 'install', 'uv==0.9'])], 'root': 'install-sync'},
    # An install was added, or activation changed: either may be what the failed install lacked.
    {'install': [cmd('install-buildx', ['get-buildx'])], 'root': 'install-bench-image'},
    {'activation': {'path': ['{cache}/bin'], 'env': {}}, 'root': 'install-bench-image'},
])
def test_an_unchanged_failed_install_passes_when_what_it_could_depend_on_changed(change: dict[str, Any]) -> None:
    triage = SimpleNamespace(roots={change['root']: 'command'}, touched_parts=['global'],
                             closure_ids=['install-uv', 'install-bench-image', 'install-sync'])
    plan = DeltaPlan(base=None, triage=triage)  # type: ignore[arg-type]
    plan.guard_part(BASE, candidate(*change.get('install', []), activation=change.get('activation')))


def test_a_capability_root_and_a_check_group_part_are_never_judged() -> None:
    plan = DeltaPlan(base=None, triage=SimpleNamespace(roots={'install-aws': 'capability'},  # type: ignore[arg-type]
                                                       closure_ids=['install-aws'], touched_parts=['global']))
    plan.guard_part(BASE, BASE)
    group = {'checks': [cmd('c00-a', ['a'])], 'criterion_checks': {}}
    PLAN.guard_part(group, group)


def check_plan(touched: list[str]) -> DeltaPlan:
    return DeltaPlan(base=None, triage=SimpleNamespace(roots={'c00-a': 'command'},  # type: ignore[arg-type]
                                                       closure_ids=['c00-a', 'install-sync'], touched_parts=touched))


WHOLE = {**BASE, 'checks': [cmd('c00-a', ['a']), cmd('check-b', ['b'])], 'criterion_checks': {}}


def test_a_failed_check_left_as_it_ran_with_nothing_else_changed_is_corrected() -> None:
    with pytest.raises(EnvironmentDiscoveryError, match='c00-a') as raised:
        check_plan(['global', 'c00-']).guard_part(WHOLE, WHOLE)
    assert raised.value.code == REPAIR_ROOT_UNCHANGED


def test_a_failed_check_left_as_it_ran_passes_when_an_install_it_may_rely_on_changed() -> None:
    synced = {**WHOLE, 'install': [cmd('install-sync', ['uv', 'sync', '--frozen']) if item['id'] == 'install-sync' else item
                                   for item in WHOLE['install']]}
    check_plan(['global', 'c00-']).guard_part(WHOLE, synced)


def test_a_check_group_judges_its_failed_check_only_when_the_global_part_cannot_change() -> None:
    group = {'checks': [cmd('c00-a', ['a'])], 'criterion_checks': {}}
    with pytest.raises(EnvironmentDiscoveryError, match='c00-a'):
        check_plan(['c00-']).guard_part(group, group)
    check_plan(['global', 'c00-']).guard_part(group, group)
    check_plan(['c00-']).guard_part(group, {**group, 'checks': [cmd('c00-a', ['a', '--fixed'])]})


def test_a_failed_check_already_retired_is_never_judged_unchanged() -> None:
    """nare-native-032: replay-done-verified, retired since gen3, still failed in gen4 and so was a root. Its only legal
    form is the retired record as it ran, so the guard refused every reply fourteen times and the delta fell back on
    attempt_schema_limit. A retired command obliges nothing; a required one in the same position is still refused."""
    retired = {**WHOLE, 'checks': [cmd('c00-a', ['a'], required=False), cmd('check-b', ['b'])]}
    plan = check_plan(['global', 'c00-'])
    plan.guard_part(retired, retired)
    with pytest.raises(EnvironmentDiscoveryError, match='c00-a'):
        plan.guard_part(WHOLE, WHOLE)
