"""Identical checks written by different parts are merged by the daemon, not bounced back to authors who cannot see them.

Dettmore e2e-03 spent all three merge rounds on duplicate_command_content: each check-group author sees only its own
part, so two parts independently wrote the same command and no author could correct it.
"""
from __future__ import annotations

import pytest

from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_shard_construction import ShardChecks, merge_parts


def _cmd(identifier, *argv, **fields):
    return {'id': identifier, 'argv': list(argv or ['true']), **fields}


def _part(checks, mapping):
    return ShardChecks.model_validate({'checks': checks, 'criterion_checks': mapping})


def test_a_group_copy_of_a_global_check_becomes_the_global_one_with_every_obligation_kept():
    global_data = {'install': [_cmd('install-a')], 'checks': [_cmd('check-env', 'env'), _cmd('check-rspec', 'rspec',
                                                                 depends_on=['install-a'], timeout_seconds=600)],
                   'requirements': [{'id': 'req', 'description': 'd', 'source': 'README.md', 'checks': ['c01-rspec']}]}
    c00 = _part([_cmd('c00-lint', 'lint')], {'S1': ['c00-lint']})
    c01 = _part([_cmd('c01-rspec', 'rspec', depends_on=['check-env'], timeout_seconds=1800, required=True),
                 _cmd('c01-after', 'after', depends_on=['c01-rspec'])], {'S2': ['c01-rspec', 'c01-after']})
    merged = merge_parts(global_data, [c00, c01])
    assert [check['id'] for check in merged['checks']] == ['check-env', 'check-rspec', 'c00-lint', 'c01-after']
    kept = merged['checks'][1]
    assert kept['depends_on'] == ['install-a', 'check-env']
    assert kept['timeout_seconds'] == 1800 and kept['required'] is True
    assert merged['criterion_checks'] == {'S1': ['c00-lint'], 'S2': ['check-rspec', 'c01-after']}
    assert merged['checks'][3]['depends_on'] == ['check-rspec']
    assert merged['requirements'][0]['checks'] == ['check-rspec']
    assert global_data['checks'][1]['timeout_seconds'] == 600


def test_a_group_copy_of_a_global_check_keeps_the_global_one():
    global_data = {'checks': [_cmd('check-ruby', 'ruby', '-v')]}
    c00 = _part([_cmd('c00-ruby', 'ruby', '-v')], {'S1': ['c00-ruby']})
    merged = merge_parts(global_data, [c00])
    assert [check['id'] for check in merged['checks']] == ['check-ruby']
    assert merged['criterion_checks'] == {'S1': ['check-ruby']}


@pytest.mark.parametrize('case', ['two_groups', 'group_dependency', 'cycle'])
def test_copies_the_parts_could_not_reference_after_a_merge_are_left_to_the_authors(case):
    """Each part must still validate alone after a merge: a later repair splits the merged manifest back into parts."""
    global_data = {'checks': [_cmd('check-db', 'db')]}
    if case == 'two_groups':
        parts = [_part([_cmd('c00-x', 'x')], {'S1': ['c00-x']}), _part([_cmd('c01-x', 'x')], {'S2': ['c01-x']})]
    elif case == 'group_dependency':
        global_data['checks'].append(_cmd('check-x', 'x'))
        parts = [_part([_cmd('c00-setup', 's'), _cmd('c00-x', 'x', depends_on=['c00-setup'])], {'S1': ['c00-x']})]
    else:
        global_data['checks'] = [_cmd('check-x', 'x'), _cmd('check-db', 'db', depends_on=['check-x'])]
        parts = [_part([_cmd('c00-x', 'x', depends_on=['check-db'])], {'S1': ['c00-x']})]
    merged = merge_parts(global_data, parts)
    assert len(merged['checks']) == len(global_data['checks']) + sum(len(part.checks) for part in parts)


def test_checks_differing_in_scope_or_env_are_not_merged():
    c00 = _part([_cmd('c00-a', 'x', scope=['dev'])], {'S1': ['c00-a']})
    c01 = _part([_cmd('c01-a', 'x', scope=['ci'])], {'S2': ['c01-a']})
    merged = merge_parts({'checks': []}, [c00, c01])
    assert [check['id'] for check in merged['checks']] == ['c00-a', 'c01-a']


def test_a_true_id_collision_is_still_an_error():
    c00 = _part([_cmd('c00-a', 'x')], {'S1': ['c00-a']})
    with pytest.raises(EnvironmentDiscoveryError):
        merge_parts({'checks': [_cmd('c00-a', 'y')]}, [c00])


def test_a_global_check_relying_on_defaults_keeps_them_when_a_shorter_optional_copy_is_merged():
    global_data = {'checks': [_cmd('check-health', 'health')]}
    c00 = _part([_cmd('c00-health', 'health', timeout_seconds=30, required=False)], {'S1': ['c00-health']})
    kept, = merge_parts(global_data, [c00])['checks']
    assert kept['id'] == 'check-health' and kept['timeout_seconds'] == 180 and kept['required'] is True
