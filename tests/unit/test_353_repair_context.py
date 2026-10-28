"""A delta repair corrects out-of-closure edits in its own phase, and a full repair's prompt carries no duplicates.

Dettmore e2e-02: the delta's global author rewrote five checks it had only seen as consequence ids; the merge
rejected it and fell back to the full repair, whose 939 KB prompt (a nested second scope contract, three identical
evidence indexes and the whole review history) then overflowed the model's context window.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_delta import DeltaPlan


def _plan(closure, *, requirements=(), criteria=()):
    triage = SimpleNamespace(closure_ids=list(closure), touched_parts=[], requirement_ids=list(requirements),
                             criterion_ids=list(criteria))
    return DeltaPlan(base=SimpleNamespace(), triage=triage)


def _cmd(identifier, *argv, **fields):
    return {'id': identifier, 'argv': list(argv or ['true']), **fields}


def _part(**changes):
    part = {'requirements': [{'id': 'req-tool', 'description': 'tool', 'source': 'README.md', 'checks': ['check-tool']}],
            'services': [{'id': 'db', 'start': _cmd('db-start'), 'health': _cmd('db-health'), 'stop': _cmd('db-stop')}],
            'activation': {'path': ['{cache}/bin'], 'env': {'A': '1'}},
            'install': [_cmd('install-tool', 'a')],
            'checks': [_cmd('check-tool', 'b'), _cmd('check-other', 'c')]}
    part.update(changes)
    return part


def test_a_delta_author_may_change_closure_records_and_add_new_ones():
    _plan({'install-tool'}).guard_part(_part(), _part(install=[_cmd('install-tool', 'a2'), _cmd('install-new', 'n')]))


def test_a_delta_author_changing_a_frozen_record_gets_a_named_correction():
    changed = _part(checks=[_cmd('check-tool', 'b2'), _cmd('check-other', 'c')])
    with pytest.raises(EnvironmentDiscoveryError) as frozen:
        _plan({'install-tool'}).guard_part(_part(), changed)
    assert frozen.value.code == 'repair_outside_closure'
    assert 'check-tool' in str(frozen.value) and 'check-other' not in str(frozen.value)


def test_removing_a_frozen_record_or_rewriting_frozen_activation_is_refused():
    plan = _plan({'install-tool'})
    with pytest.raises(EnvironmentDiscoveryError, match='check-other'):
        plan.guard_part(_part(), _part(checks=[_cmd('check-tool', 'b')]))
    with pytest.raises(EnvironmentDiscoveryError, match='activation'):
        plan.guard_part(_part(), _part(activation={'path': ['{cache}/other'], 'env': {'A': '1'}}))


def test_a_service_whose_lifecycle_command_failed_may_be_repaired():
    """Triage puts lifecycle command ids in the closure, not the service id."""
    repaired = _part(services=[{'id': 'db', 'start': _cmd('db-start'), 'health': _cmd('db-health', timeout_seconds=90),
                                'stop': _cmd('db-stop')}])
    _plan({'db-health'}).guard_part(_part(), repaired)


def test_an_implicated_requirement_may_change_and_a_frozen_one_may_gain_added_checks():
    grown = _part(requirements=[{'id': 'req-tool', 'description': 'tool', 'source': 'README.md',
                                 'checks': ['check-tool', 'check-new']}],
                  checks=[_cmd('check-tool', 'b'), _cmd('check-other', 'c'), _cmd('check-new', 'd')])
    _plan({'install-tool'}).guard_part(_part(), grown)
    reworded = _part(requirements=[{'id': 'req-tool', 'description': 'tool, pinned', 'source': 'README.md',
                                    'checks': ['check-tool']}])
    _plan({'install-tool'}, requirements={'req-tool'}).guard_part(_part(), reworded)


def test_activation_may_gain_path_entries_and_environment_keys():
    widened = _part(activation={'path': ['{cache}/bin', '{cache}/tool/bin'], 'env': {'A': '1', 'B': '2'}})
    _plan({'install-tool'}).guard_part(_part(), widened)


def test_delta_instructions_name_consequences_as_frozen():
    from coordinare.services.environment_discovery import (
        DELTA_GLOBAL_INSTRUCTION,
        DELTA_REPAIR_INSTRUCTION,
    )

    for instruction in (DELTA_GLOBAL_INSTRUCTION, DELTA_REPAIR_INSTRUCTION):
        assert 'consequences' in instruction and 'frozen' in instruction


def test_the_prompt_ledger_keeps_every_record_and_only_the_latest_review():
    from coordinare.services.environment_discovery import _ledger_for_prompt

    ledger = SimpleNamespace(model_dump=lambda mode: {'records': [{'id': 'F0001'}, {'id': 'F0002'}],
                                                      'reviews': [{'sequence': 1}, {'sequence': 2}, {'sequence': 3}]})
    trimmed = _ledger_for_prompt(ledger)
    assert trimmed['records'] == [{'id': 'F0001'}, {'id': 'F0002'}]
    assert trimmed['reviews'] == [{'sequence': 3}]


def test_identical_evidence_indexes_are_sent_once():
    from coordinare.services.environment_discovery import _dedupe_evidence

    index = [{'path': 'Gemfile', 'digest': 'a' * 64, 'size': 10, 'criteria': ['S1']}]
    content = {'bound_scope_inputs': index, 'mandatory_evidence': list(index), 'mandatory_finding_evidence': list(index),
               'other': 1}
    deduped = _dedupe_evidence(content)
    assert deduped['bound_scope_inputs'] == index
    assert deduped['mandatory_evidence'] == {'same_as': 'bound_scope_inputs'}
    assert deduped['mandatory_finding_evidence'] == {'same_as': 'bound_scope_inputs'}
    assert deduped['other'] == 1
    different = {'bound_scope_inputs': index, 'mandatory_evidence': [{'path': 'other'}]}
    assert _dedupe_evidence(different) == different


def test_the_full_repair_previous_manifest_omits_the_nested_scope_contract():
    from coordinare.services.environment_discovery import _previous_for_prompt

    previous = SimpleNamespace(model_dump=lambda mode, exclude=None: {
        key: value for key, value in {'scope_contract': {'big': 'x' * 1000}, 'scope_contract_digest': 'd' * 64,
                                      'checks': []}.items() if not exclude or key not in exclude})
    data = _previous_for_prompt(previous)
    assert 'scope_contract' not in data and data['scope_contract_digest'] == 'd' * 64
    assert len(json.dumps(data)) < 200


def test_a_group_author_is_guarded_on_its_own_checks_and_mappings():
    base = {'checks': [_cmd('c01-failed', 'x'), _cmd('c01-other', 'y')], 'criterion_checks': {'S1': ['c01-failed']}}
    plan = _plan({'c01-failed'})
    plan.guard_part(base, {'checks': [_cmd('c01-failed', 'x2'), _cmd('c01-other', 'y'), _cmd('c01-new', 'z')],
                           'criterion_checks': {'S1': ['c01-failed', 'c01-new']}})
    with pytest.raises(EnvironmentDiscoveryError, match='c01-other'):
        plan.guard_part(base, {'checks': [_cmd('c01-failed', 'x'), _cmd('c01-other', 'y2')],
                               'criterion_checks': {'S1': ['c01-failed']}})
