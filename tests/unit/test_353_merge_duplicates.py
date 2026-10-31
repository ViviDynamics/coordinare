"""Identical checks written by different parts are merged by the daemon, not bounced back to authors who cannot see them.

Dettmore e2e-03 spent all three merge rounds on duplicate_command_content: each check-group author sees only its own
part, so two parts independently wrote the same command and no author could correct it.
"""
from __future__ import annotations

import pytest

from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_shard_construction import FoldPolicy, ShardChecks, merge_parts


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


@pytest.mark.parametrize('case', ['group_dependency', 'cycle'])
def test_copies_the_parts_could_not_reference_after_a_merge_are_left_to_the_authors(case):
    """Each part must still validate alone after a merge: a later repair splits the merged manifest back into parts."""
    global_data = {'checks': [_cmd('check-db', 'db')]}
    if case == 'group_dependency':
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


def test_an_executed_group_copy_is_never_dropped():
    """Execution preservation requires every executed required check to survive under its own id; the new global copy
    is the one that goes."""
    c00 = _part([_cmd('c00-health', 'health')], {'S1': ['c00-health']})
    merged = merge_parts({'checks': [_cmd('env-x', 'x'), _cmd('check-health', 'health')]}, [c00],
                         policy=FoldPolicy(executed={'c00-health'}))
    assert [check['id'] for check in merged['checks']] == ['env-x', 'c00-health']


def test_every_fold_is_reported_so_a_required_id_can_follow_it():
    folded: dict[str, str] = {}
    c00 = _part([_cmd('c00-jq', 'jq'), _cmd('c00-own', 'own')], {'S1': ['c00-jq', 'c00-own']})
    merge_parts({'checks': [_cmd('env-jq', 'jq')]}, [c00], folded=folded)
    assert folded == {'c00-jq': 'env-jq'}


def test_an_executed_global_check_absorbs_a_copy_only_when_nothing_but_its_timeout_grows():
    global_data = {'checks': [_cmd('check-ready', 'ready'), _cmd('check-health', 'health', timeout_seconds=60)]}
    longer = _part([_cmd('c00-health', 'health', timeout_seconds=600)], {'S1': ['c00-health']})
    merged = merge_parts(global_data, [longer], policy=FoldPolicy(executed={'check-health'}))
    assert [check['id'] for check in merged['checks']] == ['check-ready', 'check-health']
    assert merged['checks'][1]['timeout_seconds'] == 600
    rewired = _part([_cmd('c00-health', 'health', depends_on=['check-ready'])], {'S1': ['c00-health']})
    merged = merge_parts(global_data, [rewired], policy=FoldPolicy(executed={'check-health'}))
    assert [check['id'] for check in merged['checks']] == ['check-ready', 'check-health', 'c00-health']


def test_a_copy_an_executed_check_depends_on_is_not_folded():
    """Folding would rewrite the executed dependent's depends_on, which execution preservation refuses."""
    global_data = {'checks': [_cmd('check-ready', 'ready')]}
    c00 = _part([_cmd('c00-ready', 'ready', required=False), _cmd('c00-test', 'test', depends_on=['c00-ready'])],
                {'S1': ['c00-test']})
    merged = merge_parts(global_data, [c00], policy=FoldPolicy(executed={'c00-ready', 'c00-test'}))
    assert [check['id'] for check in merged['checks']] == ['check-ready', 'c00-ready', 'c00-test']
    assert merge_parts(global_data, [c00])['checks'][1]['depends_on'] == ['check-ready']


def test_a_fold_leaves_every_list_not_naming_the_copy_untouched():
    """An unrelated list may be an executed check's, which may not change; a self-reference is validation's to reject."""
    global_data = {'checks': [_cmd('check-ready', 'ready'), _cmd('check-health', 'health')]}
    c00 = _part([_cmd('c00-test', 'test', depends_on=['check-ready', 'check-ready']),
                 _cmd('c00-loop', 'loop', depends_on=['c00-loop'])], {'S1': ['c00-test', 'c00-test']})
    c01 = _part([_cmd('c01-health', 'health', required=False),
                 _cmd('c01-after', 'after', depends_on=['c01-after', 'c01-health', 'check-health'])], {'S2': ['c01-health']})
    merged = merge_parts(global_data, [c00, c01])
    by_id = {check['id']: check for check in merged['checks']}
    assert 'c01-health' not in by_id
    assert by_id['c00-test']['depends_on'] == ['check-ready', 'check-ready']
    assert by_id['c00-loop']['depends_on'] == ['c00-loop']
    assert merged['criterion_checks']['S1'] == ['c00-test', 'c00-test']
    assert by_id['c01-after']['depends_on'] == ['c01-after', 'check-health']


def test_a_copy_that_changes_its_retained_obligation_is_not_folded():
    """Folding it would lose the retained scope; the obligation rule must correct its author instead."""
    from coordinare_service_inference.environment.models import Command

    retained = {'c00-jq': Command.model_validate(_cmd('c00-jq', 'jq'))}
    c00 = _part([_cmd('c00-jq', 'jq', scope=['ci'])], {'S1': ['c00-jq']})
    merged = merge_parts({'checks': [_cmd('env-jq', 'jq', scope=['ci'])]}, [c00], policy=FoldPolicy(retained=retained))
    assert [check['id'] for check in merged['checks']] == ['env-jq', 'c00-jq']
    unchanged = _part([_cmd('c00-jq', 'jq')], {'S1': ['c00-jq']})
    assert [check['id'] for check in merge_parts({'checks': [_cmd('env-jq', 'jq')]}, [unchanged], policy=FoldPolicy(retained=retained))['checks']] == ['env-jq']


def test_in_a_repair_a_copy_folds_only_when_the_kept_check_still_replaces_the_prior_one():
    """Repair preservation accepts a dropped prior check only as an alias of an identical kept command."""
    from coordinare_service_inference.environment.models import Command

    prior = {'c00-tool': [Command.model_validate(_cmd('c00-tool', 'tool', '--old'))]}
    corrected = _part([_cmd('c00-tool', 'tool', '--new')], {'S1': ['c00-tool']})
    merged = merge_parts({'checks': [_cmd('tool', 'tool', '--new')]}, [corrected], policy=FoldPolicy(prior=prior))
    assert [check['id'] for check in merged['checks']] == ['tool', 'c00-tool']
    unchanged = _part([_cmd('c00-tool', 'tool', '--old')], {'S1': ['c00-tool']})
    folded: dict[str, str] = {}
    merge_parts({'checks': [_cmd('tool', 'tool', '--old')]}, [unchanged], policy=FoldPolicy(prior=prior), folded=folded)
    assert folded == {'c00-tool': 'tool'}


def test_a_copy_folds_only_when_it_replaces_every_prior_version():
    """M1 had --old, M2 corrected it to --new: preservation still judges the candidate against M1."""
    from coordinare_service_inference.environment.models import Command

    prior = {'c00-tool': [Command.model_validate(_cmd('c00-tool', 'tool', '--old')),
                          Command.model_validate(_cmd('c00-tool', 'tool', '--new'))]}
    corrected = _part([_cmd('c00-tool', 'tool', '--new')], {'S1': ['c00-tool']})
    folded: dict[str, str] = {}
    merged = merge_parts({'checks': [_cmd('tool', 'tool', '--new')]}, [corrected], policy=FoldPolicy(prior=prior), folded=folded)
    assert folded == {} and [check['id'] for check in merged['checks']] == ['tool', 'c00-tool']


def test_identical_checks_from_two_groups_become_one_new_global_check():
    """e2e-10: c00 and c01 each wrote the full cucumber suite; neither may name the other's check."""
    c00 = _part([_cmd('c00-cucumber-full', 'bundle', 'exec', 'cucumber', timeout_seconds=1800)], {'S1': ['c00-cucumber-full']})
    c01 = _part([_cmd('c01-cucumber-full', 'bundle', 'exec', 'cucumber', timeout_seconds=3600, depends_on=['check-db'])],
                {'S2': ['c01-cucumber-full']})
    folded: dict[str, str] = {}
    merged = merge_parts({'checks': [_cmd('check-db', 'db')]}, [c00, c01], folded=folded)
    ids = [check['id'] for check in merged['checks']]
    assert ids == ['check-db', 'cucumber-full']
    kept = merged['checks'][1]
    assert kept['timeout_seconds'] == 3600 and kept['required'] is True and kept['depends_on'] == ['check-db']
    assert merged['criterion_checks'] == {'S1': ['cucumber-full'], 'S2': ['cucumber-full']}
    assert folded == {'c00-cucumber-full': 'cucumber-full', 'c01-cucumber-full': 'cucumber-full'}


def test_a_shared_copy_that_cannot_fold_is_not_promoted():
    """A copy depending on its own group's check could not be declared by the global part."""
    c00 = _part([_cmd('c00-setup', 'setup'), _cmd('c00-suite', 'suite', depends_on=['c00-setup'])], {'S1': ['c00-suite']})
    c01 = _part([_cmd('c01-suite', 'suite')], {'S2': ['c01-suite']})
    merged = merge_parts({'checks': []}, [c00, c01])
    assert [check['id'] for check in merged['checks']] == ['c00-setup', 'c00-suite', 'c01-suite']


def test_a_promoted_name_never_collides_and_split_remainders_are_one_group():
    taken = _part([_cmd('c00-lint', 'lint')], {'S1': ['c00-lint']})
    other = _part([_cmd('c01-lint', 'lint')], {'S2': ['c01-lint']})
    merged = merge_parts({'checks': [_cmd('lint', 'other-lint')]}, [taken, other])
    assert [check['id'] for check in merged['checks']] == ['lint', 'lint-shared-1']
    same_group = [_part([_cmd('c00-x', 'x')], {'S1': ['c00-x']}), _part([_cmd('c00a-x', 'x')], {'S2': ['c00a-x']})]
    assert [check['id'] for check in merge_parts({'checks': []}, same_group)['checks']] == ['c00-x', 'c00a-x']


def test_a_promoted_name_never_reuses_a_scope_criterion_id():
    criterion = 'S' + '0123456789abcdef'
    c00 = _part([_cmd(f'c00-{criterion}', 'suite')], {'S1': [f'c00-{criterion}']})
    c01 = _part([_cmd(f'c01-{criterion}', 'suite')], {'S2': [f'c01-{criterion}']})
    ids = [check['id'] for check in merge_parts({'checks': []}, [c00, c01])['checks']]
    assert ids == [f'{criterion}-shared-1']


def test_a_name_with_nested_group_prefixes_or_at_the_length_limit_stays_valid():
    nested = [_part([_cmd('c00-c01-suite', 'suite')], {'S1': ['c00-c01-suite']}),
              _part([_cmd('c01-suite', 'suite')], {'S2': ['c01-suite']})]
    assert [check['id'] for check in merge_parts({'checks': []}, nested)['checks']] == ['suite']
    long_name = 'x' * 124
    long_parts = [_part([_cmd(f'c00-{long_name}', 'long')], {'S1': [f'c00-{long_name}']}),
                  _part([_cmd(f'c01-{long_name}', 'long')], {'S2': [f'c01-{long_name}']})]
    merged = merge_parts({'checks': [_cmd(long_name, 'other')]}, long_parts)
    promoted = [check['id'] for check in merged['checks'] if check['id'] != long_name]
    assert len(promoted) == 1 and len(promoted[0]) <= 128 and promoted[0].endswith('-shared-1')


def test_new_global_copies_of_executed_group_checks_are_dropped_for_them():
    """Dettmore e2e-12: a full repair's global author renamed its copies of every group check; the groups, told to keep
    their executed ids, rewrote them each round until the merge rounds ran out."""
    global_data = {'checks': [_cmd('env-ruby', 'ruby'), _cmd('rspec-full-run', 'rspec', timeout_seconds=3600),
                              _cmd('repo-env-ready', 'cp', 'env')]}
    c00 = _part([_cmd('c00-rspec', 'rspec', timeout_seconds=1800, required=True),
                 _cmd('c00-repo-env', 'cp', 'env')], {'S1': ['c00-rspec', 'c00-repo-env']})
    folded: dict[str, str] = {}
    merged = merge_parts(global_data, [c00], folded=folded,
                         policy=FoldPolicy(executed={'c00-rspec', 'c00-repo-env'}))
    assert [check['id'] for check in merged['checks']] == ['env-ruby', 'c00-rspec', 'c00-repo-env']
    assert merged['checks'][1]['timeout_seconds'] == 3600 and merged['checks'][1]['required'] is True
    assert folded == {'rspec-full-run': 'c00-rspec', 'repo-env-ready': 'c00-repo-env'}
    assert merged['criterion_checks'] == {'S1': ['c00-rspec', 'c00-repo-env']}


@pytest.mark.parametrize('case', ['requirement', 'dependent', 'executed', 'retained', 'prior', 'more_deps',
                                  'stricter', 'not_executed'])
def test_a_global_copy_anything_could_miss_is_kept(case):
    from coordinare_service_inference.environment.models import Command
    copy = _cmd('rspec-run', 'rspec', **({'depends_on': ['env-ruby']} if case == 'more_deps' else {}))
    global_data = {'checks': [_cmd('env-ruby', 'ruby'), copy]}
    if case == 'requirement':
        global_data['requirements'] = [{'id': 'req', 'description': 'd', 'source': 'README.md', 'checks': ['rspec-run']}]
    if case == 'dependent':
        global_data['checks'].append(_cmd('after', 'after', depends_on=['rspec-run']))
    executed = {'c00-rspec'} if case != 'not_executed' else set()
    policy = FoldPolicy(executed=executed | ({'rspec-run'} if case == 'executed' else set()),
                        retained={'rspec-run': Command.model_validate(copy)} if case == 'retained' else {},
                        prior={'rspec-run': [Command.model_validate(copy)]} if case == 'prior' else {})
    c00 = _part([_cmd('c00-rspec', 'rspec', **({'required': False} if case == 'stricter' else {}))],
                {'S1': ['c00-rspec']} if case != 'stricter' else {})
    folded: dict[str, str] = {}
    merged = merge_parts(global_data, [c00], folded=folded, policy=policy)
    ids = [check['id'] for check in merged['checks']]
    if case == 'not_executed':
        # The ordinary fold keeps the global check for a copy that never ran.
        assert folded == {'c00-rspec': 'rspec-run'} and 'c00-rspec' not in ids
    else:
        assert 'rspec-run' in ids and 'c00-rspec' in ids and 'rspec-run' not in folded


def test_a_kept_group_check_is_never_read_as_a_promotion():
    from coordinare.services.environment_discovery import _promoted_sources
    global_data = {'checks': [_cmd('rspec-run', 'rspec')]}
    assert _promoted_sources({'rspec-run': 'c00-rspec'}, global_data) == {}
    assert _promoted_sources({'c00-x': 'x-shared', 'c01-x': 'x-shared'}, global_data) == {'x-shared': ['c00-x', 'c01-x']}


def test_a_global_copy_a_criterion_maps_to_is_kept():
    """Review: c01 mapping a criterion to the global copy could not name c00's check after a split."""
    global_data = {'checks': [_cmd('env-ruby', 'ruby'), _cmd('suite-copy', 'suite')]}
    c00 = _part([_cmd('c00-suite', 'suite')], {'S1': ['c00-suite']})
    c01 = _part([_cmd('c01-own', 'own')], {'S2': ['suite-copy', 'c01-own']})
    folded: dict[str, str] = {}
    merged = merge_parts(global_data, [c00, c01], folded=folded, policy=FoldPolicy(executed={'c00-suite'}))
    assert 'suite-copy' in [check['id'] for check in merged['checks']] and folded == {}


def test_the_last_global_check_is_never_dropped():
    """A global part needs a check of its own to validate alone after a split."""
    global_data = {'checks': [_cmd('suite-copy', 'suite'), _cmd('lint-copy', 'lint')]}
    c00 = _part([_cmd('c00-suite', 'suite'), _cmd('c00-lint', 'lint')], {'S1': ['c00-suite', 'c00-lint']})
    folded: dict[str, str] = {}
    merged = merge_parts(global_data, [c00], folded=folded, policy=FoldPolicy(executed={'c00-suite', 'c00-lint'}))
    assert len(folded) == 1
    assert sum(1 for check in merged['checks'] if not check['id'].startswith('c00-')) == 1


def test_a_global_check_that_kept_another_groups_copy_is_never_dropped():
    global_data = {'checks': [_cmd('env-ruby', 'ruby'), _cmd('suite-run', 'suite')]}
    c00 = _part([_cmd('c00-suite', 'suite')], {'S1': ['c00-suite']})
    c01 = _part([_cmd('c01-suite', 'suite')], {'S2': ['c01-suite']})
    folded: dict[str, str] = {}
    merged = merge_parts(global_data, [c00, c01], folded=folded, policy=FoldPolicy(executed={'c00-suite'}))
    assert folded == {'c01-suite': 'suite-run'}
    assert {'suite-run', 'c00-suite'} <= {check['id'] for check in merged['checks']}


def test_a_full_repairs_global_author_sees_only_the_global_part():
    """Dettmore e2e-13: shown the whole failed manifest, the global author copied the 38 group checks into its part."""
    from coordinare.services.environment_shard_construction import CheckGroup, global_view
    data = {'checks': [_cmd('env-ruby', 'ruby'), _cmd('c00-suite', 'suite'), _cmd('c01a-lint', 'lint')],
            'criterion_checks': {'S1': ['c00-suite']}, 'requirements': [{'id': 'req', 'checks': ['env-ruby']}]}
    view = global_view(data, [CheckGroup('c00-', ()), CheckGroup('c01-', ())])
    assert [check['id'] for check in view['checks']] == ['env-ruby']
    assert view['criterion_checks'] == {} and view['requirements'] == data['requirements']
    assert len(data['checks']) == 3
