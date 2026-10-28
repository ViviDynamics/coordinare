"""Two carrier mistakes are corrected while authoring, not after an install has spent its session on them.

Dettmore e2e-02 and e2e-04 both began with `apt-get install`, which the private-runtime carrier refuses (no Linux
capabilities: apt cannot drop privileges), so everything after it never ran. e2e-04's maxio-mock start and stop ran
`pkill -f bin/maxio-mock` inside a longer shell script, whose own command line matches the pattern: both killed
themselves (exit 143).
"""
from __future__ import annotations

import pytest
from coordinare_service_inference.environment.models import Activation, Command
from coordinare_service_inference.environment.static_rules import validate_static_rules
from pydantic_core import PydanticCustomError

NO_ACTIVATION = Activation(path=[], env={})


def _cmd(identifier, *argv):
    return Command(id=identifier, argv=list(argv))


def _rejected(install=(), others=(), *, system_packages=False):
    with pytest.raises(PydanticCustomError) as caught:
        validate_static_rules(list(install), NO_ACTIVATION, 'linux/aarch64', others=list(others),
                              system_packages=system_packages)
    return caught.value


@pytest.mark.parametrize('script', [
    'set -eu; apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y build-essential',
    'sudo apt install -y libpq-dev',
    'dpkg -i /tmp/pkg.deb',
    'apk add --no-cache postgresql-client',
    'dnf -y install gcc',
])
def test_a_system_package_install_is_corrected_in_a_carrier_without_capabilities(script):
    error = _rejected([_cmd('install-deps', 'bash', '-c', script)])
    assert error.type == 'system_package_install'
    assert 'install-deps' in str(error.context)


def test_extracting_a_package_into_the_cache_is_allowed():
    validate_static_rules([_cmd('install-lib', 'bash', '-c', 'apt-get download libpq5 && dpkg -x libpq5*.deb {cache}/pq')],
                          NO_ACTIVATION, 'linux/aarch64', system_packages=False)


def test_a_carrier_that_can_install_packages_is_not_judged():
    validate_static_rules([_cmd('install-deps', 'apt-get', 'install', '-y', 'gcc')], NO_ACTIVATION, 'linux/aarch64',
                          system_packages=True)


@pytest.mark.parametrize('script', [
    'pkill -f bin/maxio-mock >/dev/null 2>&1 || true; nohup bin/maxio-mock &',
    'pkill -f bin/maxio-mock || true; exit 0',
    'for p in $(pgrep -f puma); do kill $p; done',
    'pkill --full -9 worker; echo stopped',
])
def test_a_full_command_line_match_inside_a_longer_shell_script_is_corrected(script):
    error = _rejected(others=[_cmd('mock-stop', 'bash', '-c', script)])
    assert error.type == 'self_matching_process_pattern'
    assert 'mock-stop' in str(error.context)


@pytest.mark.parametrize('argv', [
    ['bash', '-c', 'pkill -f bin/maxio-mock'],
    ['pkill', '-f', 'bin/maxio-mock'],
    ['bash', '-c', "pkill -f '[b]in/maxio-mock' || true; exit 0"],
    ['bash', '-c', 'kill "$(cat {scratch}/mock.pid)" || true; exit 0'],
    ['bash', '-c', 'pkill maxio-mock || true'],
])
def test_matches_that_cannot_meet_their_own_shell_are_allowed(argv):
    validate_static_rules([], NO_ACTIVATION, 'linux/aarch64', others=[_cmd('mock-stop', *argv)], system_packages=False)


def test_the_carrier_guidance_states_both_rules_and_the_teardown_duty():
    from types import SimpleNamespace

    from coordinare.services.environment_capabilities import carrier_process_policy

    resources = SimpleNamespace(max_seconds=7200, cpus=8)
    policy = carrier_process_policy(SimpleNamespace(runtime=SimpleNamespace(resources=resources)))
    assert 'apt-get or apt install' in policy['installation_guidance']
    assert 'may require' not in policy['installation_guidance']
    assert 'docker rm -f -v' in policy['runtime_ordering_guidance']
    assert 'pid to a file' in policy['runtime_ordering_guidance']
    assert '[b]in' not in policy['runtime_ordering_guidance']


def test_discovery_judges_every_lifecycle_and_check_command_and_the_private_carrier_cannot_install_packages():
    from types import SimpleNamespace

    from coordinare.services.environment_discovery import _carrier_rules

    service = SimpleNamespace(init=None, start=_cmd('db-start', 'true'), health=_cmd('db-health', 'true'),
                              stop=_cmd('db-stop', 'true'))
    manifest = SimpleNamespace(checks=[_cmd('check-a', 'true')], services=[service])
    private = _carrier_rules(manifest, SimpleNamespace(runtime=object()))
    assert [command.id for command in private['others']] == ['check-a', 'db-start', 'db-health', 'db-stop']
    assert private['system_packages'] is False
    assert _carrier_rules(manifest, SimpleNamespace(runtime=None))['system_packages'] is True


def test_both_rules_carry_correction_guidance():
    from coordinare.services.environment_admission import GRAPH_GUIDANCE

    assert 'prebuilt' in GRAPH_GUIDANCE['system_package_install']
    assert 'process id' in GRAPH_GUIDANCE['self_matching_process_pattern']


@pytest.mark.parametrize('script', [
    'apt-get install -y libpq-dev; mkdir -p "{cache}/bin"',
    'mkdir -p "{cache}/opt"; DEBIAN_FRONTEND=noninteractive apt-get install -y gcc',
])
def test_a_quoted_template_does_not_hide_a_package_install(script):
    assert _rejected([_cmd('install-deps', 'bash', '-c', script)]).type == 'system_package_install'


def test_a_quoted_template_does_not_hide_a_self_matching_pattern():
    script = 'pkill -f bin/mock || true; rm -f "{scratch}/mock.pid"'
    assert _rejected(others=[_cmd('mock-stop', 'bash', '-c', script)]).type == 'self_matching_process_pattern'


@pytest.mark.parametrize('script', [
    "pgrep -f '^/opt/app/server'; echo ready",
    'pkill -P "$(cat {scratch}/mock.pid)" -f mock || true; exit 0',
    'pgrep --parent 1 -f worker && echo up',
])
def test_anchored_or_parent_restricted_patterns_cannot_meet_their_own_shell(script):
    validate_static_rules([], NO_ACTIVATION, 'linux/aarch64', others=[_cmd('probe', 'bash', '-c', script)],
                          system_packages=False)


def test_a_check_author_is_corrected_on_a_self_matching_pattern_while_authoring():
    from coordinare.services.environment_scope import _ResponseReferenceError
    from coordinare.services.environment_shard_construction import CheckGroup, validate_group

    group = CheckGroup('c00-', ())
    data = {'shard_checks': {'checks': [{'id': 'c00-stop', 'argv': ['bash', '-c', 'pkill -f bin/mock || true; exit 0']}],
                             'criterion_checks': {}}}
    with pytest.raises(_ResponseReferenceError) as caught:
        validate_group(data, group=group, summary={'checks': [], 'readiness_ids': []})
    codes = {error['code'] for error in caught.value.errors}
    assert 'self_matching_process_pattern' in codes


@pytest.mark.parametrize('script', [
    'pkill -u root -f bin/mock || true; exit 0',
    'pkill -G root -f bin/mock || true; exit 0',
    "pkill -f '^.*bin/mock' || true; exit 0",
    "pkill -f '^bash -c pkill' || true; exit 0",
    'pkill -f bin/mock >/dev/null 2>&1 || true; exit 0',
    'pkill --signal TERM -f bin/mock 2>&1; echo stopped',
])
def test_selectors_and_anchors_that_still_reach_the_shell_are_corrected(script):
    assert _rejected(others=[_cmd('mock-stop', 'bash', '-c', script)]).type == 'self_matching_process_pattern'


@pytest.mark.parametrize('script', [
    "docker run --rm debian sh -c 'set -e; apt-get install -y gcc; gcc --version'",
    'docker exec dettmore-pg bash -c "apt-get update; apt-get install -y vim"',
])
def test_a_package_install_inside_a_guest_container_is_not_the_carriers(script):
    validate_static_rules([_cmd('install-in-container', 'bash', '-c', script)], NO_ACTIVATION, 'linux/aarch64',
                          system_packages=False)


def test_a_nested_carrier_shell_is_still_judged():
    script = 'set -eu; bash -c "apt-get update; apt-get install -y gcc"'
    assert _rejected([_cmd('install-deps', 'bash', '-c', script)]).type == 'system_package_install'


@pytest.mark.parametrize('script', [
    "pkill -f '[b]in/mock' >/dev/null 2>&1 || true; exit 0",
    "cat <<'EOF' > {scratch}/notes.txt\napt-get install -y gcc\nEOF\necho written",
    "echo '|' apt-get install -y gcc",
])
def test_the_rules_never_reject_a_command_they_misread(script):
    """Conservative by design: a redirection target, a heredoc body or a quoted operator is never read as a command."""
    validate_static_rules([_cmd('install-notes', 'bash', '-c', script)], NO_ACTIVATION, 'linux/aarch64',
                          others=[_cmd('mock-stop', 'bash', '-c', script)], system_packages=False)


def test_a_redirection_target_is_never_taken_as_the_pattern():
    script = 'pkill -f bin/mock > "[x]" || true; exit 0'
    assert _rejected(others=[_cmd('mock-stop', 'bash', '-c', script)]).type == 'self_matching_process_pattern'


def test_the_correction_recommends_a_pid_file_only():
    from coordinare.services.environment_admission import GRAPH_GUIDANCE

    assert 'pid' in GRAPH_GUIDANCE['self_matching_process_pattern']
    assert '[b]in' not in GRAPH_GUIDANCE['self_matching_process_pattern']


@pytest.mark.parametrize(('install', 'other'), [
    ("printf '%s\\n' \\; apt-get install gcc", 'true'),
    ('apt-get -s install gcc', 'true'),
    ('apk add --help', 'true'),
    ('apt-get install --download-only -y gcc', 'true'),
    ('true', "set -eu; exec pgrep -f bin/mock"),
    ('true', "pgrep -fx '/opt/app/server --port 8080' && echo ready"),
])
def test_review_r4_valid_commands_are_admitted(install, other):
    validate_static_rules([_cmd('install-x', 'bash', '-c', install)], NO_ACTIVATION, 'linux/aarch64',
                          others=[_cmd('probe-x', 'bash', '-c', other)], system_packages=False)
