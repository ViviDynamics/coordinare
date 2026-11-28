"""nare-native-043 held on its coverage revisions, with installs passing and three checks failing.

nare's sandbox tests bind-mounted a mkdtemp directory into containers the guest daemon ran; the guest, which shares
only {repo}, {cache} and {scratch}, served it as an empty root-owned directory ('/work/.git: Permission denied'), and no
prompt said which paths the guest sees. Reviewers then held findings open for want of results of checks the candidate
had just added, which nothing runs before acceptance, while the author's three trials stayed in its own conversation; a
c00 reviewer kept a finding open about a c01 check; the delta's fallback recorded no reason; and the installer's
read-only {cache}, only its write bits removed, was writable on a macOS host bind, so uv checks passed there that the
consumer's read-only mount fails.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment import runner
from coordinare_service_inference.environment import runtime_supervisor as supervisor
from coordinare_service_inference.environment.models import Command
from coordinare_service_inference.environment.runtime_guest import (
    cloud_configuration,
    qemu_arguments,
)

from coordinare.services import environment_discovery as discovery
from coordinare.services.environment_attempt import AttemptBudget
from coordinare.services.environment_delta import STANDING_EXCLUDED
from coordinare.services.environment_repair_trial import (
    MAX_TRIALS_PER_REPAIR,
    TrialRequest,
    record_trial,
)
from coordinare.services.environment_scope import _ResponseReferenceError
from coordinare.services.environment_shard_construction import (
    GLOBAL_OWNER,
    REVIEW_PART_PREAMBLE,
    CheckGroup,
    ReviewIds,
    validate_review_part,
)
from tests.unit.test_353_sharded_executable import make_criterion


def _arguments(tmpdir: Path | None) -> list[str]:
    resources = SimpleNamespace(memory_mib=4096, cpus=2)
    spec = SimpleNamespace(policy=SimpleNamespace(control_port=2222, resources=resources), endpoints=[])
    bundle = SimpleNamespace(emulator='/usr/bin/qemu-system-aarch64', firmware='/fw.fd', guest_platform='linux/aarch64')
    paths = {name: Path('/' + name) for name in ('disk', 'seed', 'repo', 'cache', 'scratch', 'control')}
    return qemu_arguments(spec, bundle, install=False, carrier_platform='linux/aarch64', kvm_usable=False,
                          tmpdir=tmpdir, **paths)


def test_the_commands_tmpdir_is_shared_into_the_guest_at_its_own_path():
    shared = _arguments(Path('/var/tmp/coordinare'))
    assert 'local,path=/var/tmp/coordinare,mount_tag=tmpdir,security_model=none' in shared
    assert not any('mount_tag=tmpdir' in argument for argument in _arguments(None))
    # The cache stays read-only outside an install; the TMPDIR share is always writable.
    assert 'local,path=/cache,mount_tag=cache,security_model=none,readonly=on' in shared
    script = json.loads(cloud_configuration(repo=Path('/r'), cache=Path('/c'), scratch=Path('/s'), readonly=True,
                                            tmpdir=Path('/var/tmp/coordinare')).removeprefix('#cloud-config\n'))['runcmd'][0]
    assert 'mount -t 9p -o trans=virtio,version=9p2000.L tmpdir /var/tmp/coordinare' in script
    assert 'mount -t 9p -o trans=virtio,version=9p2000.L,ro cache /c' in script
    # The guest's own /tmp tmpfs is mounted before the shares.
    assert script.index('tmpfs /tmp') < script.index('tmpdir /var/tmp/coordinare')


def test_the_guest_gets_the_tmpdir_only_where_the_carrier_has_it(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor, 'COMMAND_TMPDIR', str(tmp_path / 'absent'))
    assert supervisor.guest_tmpdir() is None
    monkeypatch.setattr(supervisor, 'COMMAND_TMPDIR', str(tmp_path))
    assert supervisor.guest_tmpdir() == tmp_path


def test_prompts_name_what_the_guest_daemon_sees():
    rule = discovery.RUNTIME_BIND_MOUNTS
    assert all(root in rule for root in ('{repo}', '{cache}', '{scratch}', 'TMPDIR'))
    assert 'empty root-owned directory' in rule and 'no image layer can make a bind-mounted path writable' in rule


def test_reviewers_are_told_an_added_check_has_not_run_and_see_the_trials():
    assert discovery.UNRUN_CHECKS_REVIEW in discovery.EXECUTABLE_REVIEW_CRITERIA
    assert 'never by itself a blocking finding' in discovery.UNRUN_CHECKS_REVIEW
    budget = AttemptBudget(max_calls=8, max_reads=8, max_coverage_revisions=1, max_schema_repairs=1)
    assert discovery.trial_observations(budget) == {}
    request = TrialRequest.model_validate({'checks': [{'id': 'c00-bench', 'argv': ['bin/bench', 'verify']}]})
    for run in range(MAX_TRIALS_PER_REPAIR + 1):
        budget.trial_runs = run + 1
        record_trial(budget, request, {'passed': False, 'results': [{'id': 'c00-bench', 'status': 'fail'}]})
    shown = discovery.trial_observations(budget)['untrusted_trial_observations']
    assert [item['trial'] for item in shown] == [2, 3, 4]
    assert shown[-1]['checks'] == ['c00-bench'] and shown[-1]['results'][0]['status'] == 'fail'
    # A trial never changes a part's reviewed standing.
    assert 'untrusted_trial_observations' in STANDING_EXCLUDED


def test_a_rejected_delta_says_why():
    budget = AttemptBudget(max_calls=8, max_reads=8, max_coverage_revisions=8, max_schema_repairs=1)
    budget.delta_revisions, budget.coverage_revisions = 1, 2
    detail = discovery.delta_rejection_detail('revise', budget, [], ['F0012', 'F0006'])
    assert detail == 'delta revisions exhausted; delta revisions 1/1; coverage revisions 2/8; open F0006,F0012'
    assert discovery.delta_rejection_detail('revise', budget, ['S3'], []).startswith('release requested; ')
    assert discovery.delta_rejection_detail('indeterminate', budget, [], ['F0001']).startswith('review verdict indeterminate')
    budget.delta_revisions = 0
    budget.coverage_revisions = 8
    assert discovery.delta_rejection_detail('revise', budget, [], []).startswith('coverage revisions exhausted')
    assert len(discovery.delta_rejection_detail('revise', budget, [], [f'F{i:04d}' for i in range(200)])) == 500


def _part_review(*affected: str) -> dict:
    finding = {'category': 'execution_definition', 'issue': 'still required', 'sources': ['a.md'],
               'affected_ids': list(affected)}
    return {'assessment': {'findings': [finding], 'decisions': [], 'capability_unavailable': False,
                           'verdict': 'revise', 'rationale': 'gap', 'sources': ['a.md']}}


def test_a_part_reviewer_raises_findings_only_about_its_own_part():
    groups = [CheckGroup('c00-', (make_criterion(1),)), CheckGroup('c01-', (make_criterion(2),))]
    inspection = SimpleNamespace(inputs={'a.md': 'a' * 64})
    known = ReviewIds(obligations={'c00-bench', 'c01-docker', 'install-uv'}, owner='c00-', groups=groups)
    with pytest.raises(_ResponseReferenceError) as caught:
        validate_review_part(_part_review('c01-docker'), inspection=inspection, owned=[], capacity=4, known=known)
    [error] = caught.value.errors
    assert error['owners'] == ['c01-'] and error['location'][-1] == 'affected_ids'
    # Its own record, one shared with another part, or no record at all (an uncovered obligation) is its to raise.
    for affected in (('c00-bench',), ('c00-bench', 'c01-docker'), ()):
        validate_review_part(_part_review(*affected), inspection=inspection, owned=[], capacity=4, known=known)
    # The global reviewer, likewise, leaves a group's checks to that group.
    known_global = ReviewIds(obligations=known.obligations, owner=GLOBAL_OWNER, groups=groups)
    validate_review_part(_part_review('install-uv'), inspection=inspection, owned=[], capacity=4, known=known_global)
    with pytest.raises(_ResponseReferenceError):
        validate_review_part(_part_review('c00-bench'), inspection=inspection, owned=[], capacity=4, known=known_global)
    assert 'part holds the records you review' in REVIEW_PART_PREAMBLE


def test_commands_in_a_read_only_tree_are_told_its_root(tmp_path):
    assert runner._READ_ONLY_ROOTS.get() == ()
    with runner._read_only_tree(tmp_path):
        assert runner._READ_ONLY_ROOTS.get() == (str(tmp_path),)
    assert runner._READ_ONLY_ROOTS.get() == ()
    assert "environment.pop('=read_only', None)" in runner._LINUX_SUPERVISOR


@pytest.mark.skipif(sys.platform != 'linux', reason='the command supervisor runs on Linux')
def test_a_check_cannot_write_the_read_only_cache(tmp_path):
    cache = tmp_path / 'cache'
    cache.mkdir()
    command = Command.model_validate({'id': 'c00-write', 'argv': ['sh', '-c', 'touch {cache}/lock']})
    with runner._read_only_tree(cache):
        result = asyncio.run(runner._execute(command, roots={'repo': str(tmp_path), 'cache': str(cache),
                                                              'scratch': str(tmp_path)},
                                             env={'PATH': '/usr/bin:/bin'}, digest='0' * 64, scope='', results={}))
    assert result.status == 'fail'
    assert not (cache / 'lock').exists()


@pytest.mark.skipif(sys.platform != 'linux', reason='the command supervisor runs on Linux')
def test_an_orphan_killed_mid_command_is_reaped_and_the_exit_code_kept(tmp_path):
    """nare-native-047: the subreaper waited only on its child, so a killed orphan stayed a zombie that kill -0 saw."""
    script = ("sh -c 'sleep 100 & echo $! > {scratch}/pid'; kill $(cat {scratch}/pid); sleep 0.5; "
              "kill -0 $(cat {scratch}/pid) 2>/dev/null && echo ALIVE || echo GONE; exit 3")
    command = Command.model_validate({'id': 'c00-orphan', 'argv': ['sh', '-c', script]})
    result = asyncio.run(runner._execute(command, roots={'repo': str(tmp_path), 'cache': str(tmp_path),
                                                          'scratch': str(tmp_path)},
                                         env={'PATH': '/usr/bin:/bin'}, digest='0' * 64, scope='', results={}))
    assert result.exit_code == 3
    assert 'GONE' in (result.output or '') and 'ALIVE' not in (result.output or '')
    assert "def reap_exited():" in runner._LINUX_SUPERVISOR
