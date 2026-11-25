"""nare-native-038 held on executable_merge_invalid with 15,600 s left: the global author rewrote check-replay-done into
c00-replay-done-jsonl's exact command, the duplicate went only to c00, which may neither drop a kept check nor edit a
global one, and all three merge rounds repeated it. The hold said nothing (error_detail None), and the rejected
delta's obligation_removed message before it was lost when the full repair took over."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment.models import ShardChecks

from coordinare.services import environment_discovery as discovery_module
from coordinare.services.environment_attempt import AttemptBudget, EnvironmentDiscoveryError
from coordinare.services.environment_shard_construction import CheckGroup, DividedConstruction
from tests.unit.test_353_sharded_executable import (
    FakeBuild,
    _duplicate_error,
    make_criterion,
    merge_with,
)


def _part(*checks: tuple[str, bool]) -> dict:
    return {'checks': [{'id': identifier, 'argv': ['run', identifier], 'required': required}
                       for identifier, required in checks], 'criterion_checks': {}}


def test_a_groups_kept_required_checks_are_pinned():
    groups = [CheckGroup('c00-', (make_criterion(1),)), CheckGroup('c01-', (make_criterion(2),))]
    build = SimpleNamespace(groups=groups, previous={'c00-': _part(('c00-replay', True), ('c00-optional', False)),
                                                     'c01-': _part(('c01-lint', True))})
    assert DividedConstruction.pinned_checks(build, groups) == {'c00-replay', 'c01-lint'}
    # A group with no previous part keeps nothing yet: it may still drop what it wrote.
    assert DividedConstruction.pinned_checks(SimpleNamespace(groups=groups, previous={}), groups) == frozenset()


def test_a_duplicate_of_a_global_check_and_a_kept_group_check_goes_to_the_global_author():
    errors = [{'location': [], 'code': 'duplicate_command_content', 'guidance': 'Keep one of the two.',
               'pairs': [['check-replay-done', 'c00-replay-done-jsonl']]}]
    parts = {prefix: (CheckGroup(prefix, ()), ShardChecks.model_validate({'checks': [], 'criterion_checks': {}}))
             for prefix in ('c00-', 'c01-')}
    judged = discovery_module._group_errors(errors, {'c00-replay-done-jsonl'})
    assert discovery_module._attributed(judged, ValueError(), parts) == []
    assert 'executed or kept check' in errors[0]['guidance']
    exc = _duplicate_error(('check-replay-done', 'c00-replay-done-jsonl'))
    assert discovery_module._global_copies(exc, {'c00-replay-done-jsonl'}) == {'check-replay-done'}


@pytest.mark.asyncio
async def test_the_merge_sends_a_pinned_pair_to_the_global_author(monkeypatch):
    calls: list[int] = []

    def validate(data):
        calls.append(1)
        if len(calls) == 1:
            raise _duplicate_error(('toolchain', 'c00-0'))
        return data

    monkeypatch.setattr(FakeBuild, 'pinned_checks', lambda _self, _groups: frozenset({'c00-0'}))
    _merged, build, phases, messages = await merge_with(monkeypatch, validate)
    # The global author revised (a second global phase) rather than c00 alone being sent back.
    assert phases == [1, 1]
    assert build.rounds == [['c00-', 'c01-'], ['c00-', 'c01-']]
    assert 'duplicate_command_content' in messages[-1]['content']


@pytest.mark.asyncio
async def test_errors_a_group_returns_unchanged_go_to_the_global_author_and_the_hold_names_them(monkeypatch):
    def validate(_data):
        raise _duplicate_error(('c01-0', 'c00-0'))

    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await merge_with(monkeypatch, validate)
    assert caught.value.code == 'executable_merge_invalid'
    assert caught.value.detail is not None
    assert 'duplicate_command_content' in caught.value.detail and 'c00-0' in caught.value.detail


@pytest.mark.asyncio
async def test_the_same_errors_after_a_global_revision_end_the_merge_early(monkeypatch):
    def validate(_data):
        raise EnvironmentDiscoveryError('repair must independently read every previous input', code='source_identity')

    with pytest.raises(EnvironmentDiscoveryError) as caught:
        await merge_with(monkeypatch, validate)
    assert caught.value.code == 'executable_merge_invalid'
    assert 'repeated the same merge errors' in str(caught.value)
    assert caught.value.detail == 'source_identity'


def test_a_merge_detail_names_codes_and_ids_only():
    errors = [{'code': 'duplicate_command_content', 'guidance': 'model-facing prose', 'pairs': [['a', 'c00-a']]},
              {'code': 'inherited_mapping_missing', 'missing': {'S1': ['c00-b']}, 'input': 'rendered candidate'}]
    detail = discovery_module.merge_detail(errors)
    assert detail == ('duplicate_command_content {"pairs":[["a","c00-a"]]}; '
                      'inherited_mapping_missing {"missing":{"S1":["c00-b"]}}')
    removed = EnvironmentDiscoveryError('criterion S2 must keep checks c00-replay', code='obligation_removed')
    assert discovery_module.merge_detail([{'code': 'obligation_removed'}], removed).endswith(
        'criterion S2 must keep checks c00-replay')
    assert len(discovery_module.merge_detail([{'code': 'x' * 900}])) == 500


def test_a_rejected_delta_keeps_what_its_merge_said_through_the_fallback():
    budget = AttemptBudget(max_calls=8, max_reads=8, max_coverage_revisions=1, max_schema_repairs=1)
    removed = EnvironmentDiscoveryError('criterion S2 must keep checks c00-replay', code='obligation_removed')
    rejected = discovery_module._delta_merge_rejected(budget, removed)
    assert rejected.code == 'repair_delta_rejected'
    assert rejected.detail == 'obligation_removed; criterion S2 must keep checks c00-replay'
    budget.repair_delta = True
    discovery_module._fall_back(budget, rejected.code, rejected.detail)
    summary = budget.summary()
    assert summary['delta_fallback_code'] == 'repair_delta_rejected'
    assert summary['delta_fallback_detail'] == rejected.detail
