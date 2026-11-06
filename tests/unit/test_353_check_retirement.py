"""A required check that failed and never passed may be retired when it is wrong in itself, and only then.

Dettmore e2e-58: an advisory audit required to find nothing, `ruby -e 'require "rspec"'` outside Bundler and a jq call
on multi-line output each failed. Executed checks are fixed, so no repair could correct them and the attempt held on
its coverage limit. Retiring keeps the check exactly as it ran, no longer required, and hands its criteria and
requirements to new checks the independent review judges; a check that ever passed stays fixed.
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import cast

import pytest
from coordinare_service_inference.environment.models import EnvironmentManifest, ShardChecks

from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_discovery import EXECUTABLE_REVIEW_INSTRUCTIONS, FIXED_REVISION
from coordinare.services.environment_obligations import (
    RETIRE_FAILED_CHECK,
    RETIREMENT_REVIEW,
    preserve_execution_obligations,
    preserve_obligations,
    restore_execution_commands,
    retirable_check_ids,
    validate_retirements,
)
from coordinare.services.environment_scope import _ResponseReferenceError
from coordinare.services.environment_shard_construction import (
    CHECK_AUTHOR_INSTRUCTIONS,
    DELTA_CHECK_INSTRUCTION,
    CheckGroup,
    Inheritance,
    validate_group,
)
from tests.unit.test_353_execution_obligations import contract, history

CRITERION = 'S1df4fa8e81d22a18'


def _retired(previous: EnvironmentManifest, *, replacement: bool = True, **changes) -> EnvironmentManifest:
    """suite kept as it ran but no longer required; a new check maps its criterion in its place."""
    data = previous.model_dump(mode='json')
    data['checks'][0] = {**data['checks'][0], 'required': False, **changes}
    mapped = ['assets']
    if replacement:
        data['checks'].append({**data['checks'][0], 'id': 'suite-corrected', 'argv': ['quality-runner', 'suite', '--fixed'],
                               'required': True})
        mapped.append('suite-corrected')
    data['criterion_checks'] = {CRITERION: mapped}
    return EnvironmentManifest.model_validate(data)


def test_only_a_required_check_that_failed_after_running_and_never_passed_is_retirable():
    previous = contract()
    source = previous.source_commit
    assert retirable_check_ids(history(previous, ran=('suite', 'assets')), source_commit=source) == {'suite'}
    # Passing in any execution keeps a check fixed for good: here suite passed first and assets failed, then the reverse.
    earlier = history(previous, ran=('suite', 'assets'))
    results = {result['id']: result for result in earlier[0]['report']['results']}
    results['suite'].update(status='pass', exit_code=0)
    results['assets'].update(status='fail', exit_code=1)
    both = [*earlier, *history(previous, ran=('suite', 'assets'))]
    assert retirable_check_ids(both, source_commit=source) == frozenset()
    # A preflight stop ran nothing, so nothing failed.
    assert retirable_check_ids(history(previous, preflight=True), source_commit=source) == frozenset()


def test_a_retired_check_with_a_replacement_passes_execution_and_coverage_preservation():
    previous = contract()
    candidate = _retired(previous)
    preserve_execution_obligations(candidate, history(previous))
    preserve_obligations(candidate, previous, {'suite'})


@pytest.mark.parametrize(('kwargs', 'detail'), [
    ({'replacement': False}, 'needs a new required check in place of suite'),
    ({'argv': ['true']}, 'must keep exactly what it ran'),
])
def test_a_retirement_that_leaves_an_obligation_unserved_or_rewrites_the_check_is_refused(kwargs, detail):
    previous = contract()
    with pytest.raises(EnvironmentDiscoveryError, match=detail) as exc:
        preserve_execution_obligations(_retired(previous, **kwargs), history(previous))
    assert exc.value.code == 'obligation_removed'


def test_a_retired_check_no_required_command_depends_on():
    previous = contract()
    data = _retired(previous).model_dump(mode='json')
    data['checks'][1]['depends_on'] = ['suite']
    with pytest.raises(EnvironmentDiscoveryError, match='still depend on suite'):
        validate_retirements(EnvironmentManifest.model_validate(data), [previous], {'suite'}, {})


def test_a_check_that_passed_cannot_be_retired():
    previous = contract()
    data = _retired(previous).model_dump(mode='json')
    # assets passed: demoting it is the old weakening, refused by execution preservation.
    data['checks'][1]['required'] = False
    data['criterion_checks'] = {CRITERION: ['suite-corrected']}
    data['checks'][0]['required'] = True
    data['criterion_checks'][CRITERION].append('suite')
    with pytest.raises(EnvironmentDiscoveryError) as exc:
        preserve_execution_obligations(EnvironmentManifest.model_validate(data), history(previous, ran=('suite', 'assets')))
    assert exc.value.code == 'obligation_removed'


def test_a_merge_restores_a_retired_checks_content_but_keeps_it_retired():
    previous = contract()
    data = _retired(previous, argv=['true']).model_dump(mode='json')
    assert restore_execution_commands(data, [previous], {'suite'}) == ['suite']
    assert data['checks'][0]['argv'] == ['quality-runner', 'suite'] and data['checks'][0]['required'] is False
    # Without retirement the executed record comes back whole, required again.
    plain = _retired(previous).model_dump(mode='json')
    restore_execution_commands(plain, [previous])
    assert plain['checks'][0]['required'] is True


def test_a_check_author_may_drop_a_retired_checks_mapping_but_no_other():
    group = cast('CheckGroup', SimpleNamespace(prefix='c00', ids={CRITERION}))
    inherited = Inheritance(own=frozenset({'c00-suite'}), required=frozenset({'c00-suite'}),
                            mapped={CRITERION: frozenset({'c00-suite'})})
    summary = {'checks': [], 'readiness_ids': []}
    part = {'checks': [{'id': 'c00-suite', 'argv': ['quality-runner', 'suite'], 'required': False},
                       {'id': 'c00-suite-corrected', 'argv': ['quality-runner', 'suite', '--fixed']}],
            'criterion_checks': {CRITERION: ['c00-suite-corrected']}}
    assert isinstance(validate_group({'shard_checks': part}, group=group, summary=summary,
                                     inherited=replace(inherited, retirable=frozenset({'c00-suite'}))), ShardChecks)
    with pytest.raises(_ResponseReferenceError) as refused:
        validate_group({'shard_checks': part}, group=group, summary=summary, inherited=inherited)
    assert 'inherited_mapping_missing' in str(refused.value.errors)


def test_authors_are_told_when_they_may_retire_and_reviewers_how_to_judge_it():
    for instructions in (CHECK_AUTHOR_INSTRUCTIONS, DELTA_CHECK_INSTRUCTION, FIXED_REVISION):
        assert RETIRE_FAILED_CHECK in instructions
    assert RETIREMENT_REVIEW in EXECUTABLE_REVIEW_INSTRUCTIONS
    assert 'Never retire a check the environment failed' in RETIRE_FAILED_CHECK
