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


def _plan(closure):
    return DeltaPlan(base=SimpleNamespace(), triage=SimpleNamespace(closure_ids=list(closure), touched_parts=[]))


def _part(**changes):
    part = {'requirements': [], 'services': [], 'activation': {'path': ['{cache}/bin'], 'env': {}},
            'install': [{'id': 'install-tool', 'argv': ['a']}],
            'checks': [{'id': 'check-tool', 'argv': ['b']}, {'id': 'check-other', 'argv': ['c']}]}
    part.update(changes)
    return part


def test_a_delta_author_may_change_closure_records_and_add_new_ones():
    plan = _plan({'install-tool'})
    plan.guard_part(_part(), _part(install=[{'id': 'install-tool', 'argv': ['a2']}, {'id': 'install-new', 'argv': ['n']}]))


def test_a_delta_author_changing_a_frozen_record_gets_a_named_correction():
    plan = _plan({'install-tool'})
    changed = _part(checks=[{'id': 'check-tool', 'argv': ['b2']}, {'id': 'check-other', 'argv': ['c']}])
    with pytest.raises(EnvironmentDiscoveryError) as frozen:
        plan.guard_part(_part(), changed)
    assert frozen.value.code == 'repair_outside_closure'
    assert 'check-tool' in str(frozen.value) and 'check-other' not in str(frozen.value)


def test_removing_a_frozen_record_or_changing_frozen_activation_is_refused():
    plan = _plan({'install-tool'})
    with pytest.raises(EnvironmentDiscoveryError, match='check-other'):
        plan.guard_part(_part(), _part(checks=[{'id': 'check-tool', 'argv': ['b']}]))
    with pytest.raises(EnvironmentDiscoveryError, match='activation'):
        plan.guard_part(_part(), _part(activation={'path': ['{cache}/other'], 'env': {}}))


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
