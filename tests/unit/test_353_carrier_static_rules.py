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


def test_extracting_a_fetched_package_into_the_cache_is_allowed():
    script = 'curl -fsSLO https://deb.debian.org/debian/pool/main/p/pkg/libpq5.deb && dpkg-deb -x libpq5.deb {cache}/pq'
    validate_static_rules([_cmd('install-lib', 'bash', '-c', script)], NO_ACTIVATION, 'linux/aarch64',
                          system_packages=False)


@pytest.mark.parametrize('script', [
    'apt-get update -qq',
    'cd {cache}/debs && apt-get download libpq5 libyaml-0-2',
    'apt download zlib1g',
    'apt-get -y install -d libssl-dev',
    'apt-get source ruby3.4',
    # Earlier review rounds admitted download-only installs as needing no installation capability; the fetch itself
    # needs capabilities this carrier lacks.
    'apt-get install --download-only -y gcc',
    'apt-get install -d libpq5',
    'apt-get install -dy libpq5',
    'apt-get install --download-only=true libpq5',
])
def test_an_apt_fetch_is_corrected_in_a_carrier_without_capabilities(script):
    """Dettmore e2e-18 and e2e-22: apt-get download and apt-get update failed with "Failed to setgroups", spending the
    first execution each time; the guidance had suggested extracting packages, which authors read as apt download."""
    error = _rejected([_cmd('install-sysroot', 'bash', '-c', script)])
    assert error.type == 'system_package_install'


@pytest.mark.parametrize('script', ['apt-cache policy libpq5', 'apt-get --help', 'apt-get -s install gcc',
                                    'apt-get --print-uris install gcc'])
def test_apt_requests_that_fetch_nothing_are_allowed(script):
    validate_static_rules([_cmd('install-plan', 'bash', '-c', script)], NO_ACTIVATION, 'linux/aarch64',
                          system_packages=False)


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
    assert 'dpkg-deb -x' in policy['installation_guidance'] and 'update and download' in policy['installation_guidance']
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
    'pkill -f bin/mock >/dev/null 2>&1 || true; exit 0',
    'pkill --signal TERM -f bin/mock 2>&1; echo stopped',
    'pkill -9 -f bin/maxio-mock; exit 0',
    'MODE=test pgrep -f bin/mock; echo ready',
])
def test_the_plain_literal_form_is_corrected_whatever_surrounds_it(script):
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
    ('true', "set -eu; exec pgrep -f bin/mock"),
    ('true', "pgrep -fx '/opt/app/server --port 8080' && echo ready"),
])
def test_review_r4_valid_commands_are_admitted(install, other):
    validate_static_rules([_cmd('install-x', 'bash', '-c', install)], NO_ACTIVATION, 'linux/aarch64',
                          others=[_cmd('probe-x', 'bash', '-c', other)], system_packages=False)


@pytest.mark.parametrize(('install', 'other'), [
    ('apt search install', 'true'),
    ('zypper search install', 'true'),
    ('true', 'pgrep -F/tmp/foo.pid worker && echo ready'),
    ('true', "pgrep -f 'worker$' && echo ready"),
    ('true', 'MODE=test exec pgrep -f bin/mock; echo ready'),
    ('true', 'pkill -u root -f bin/mock || true; exit 0'),
])
def test_review_r5_valid_or_unjudged_commands_are_admitted(install, other):
    """Only the certain forms are judged: an install subcommand, and a plain -f literal with at most a signal."""
    validate_static_rules([_cmd('install-x', 'bash', '-c', install)], NO_ACTIVATION, 'linux/aarch64',
                          others=[_cmd('probe-x', 'bash', '-c', other)], system_packages=False)


@pytest.mark.parametrize(('install', 'other'), [
    ('true', "pgrep -f 'w''orker' && echo ready"),
    ('true', 'pgrep -f foo\\ bar && echo ready'),
    ('apt-get -c install download libpq5', 'true'),
    ('apt-get -o Debug::NoLocking=1 install gcc', 'true'),
])
def test_review_r6_quoted_operands_downloads_and_valued_options_are_admitted(install, other):
    validate_static_rules([_cmd('install-x', 'bash', '-c', install)], NO_ACTIVATION, 'linux/aarch64',
                          others=[_cmd('probe-x', 'bash', '-c', other)], system_packages=False)


@pytest.mark.parametrize('script', [
    'apt-get -y install gcc',
    'apt-get -qq --no-install-recommends install gcc',
    'apk --no-cache add postgresql-client',
    'apt-get --option=Debug::NoLocking=1 install gcc',
])
def test_self_contained_options_before_the_subcommand_still_count(script):
    assert _rejected([_cmd('install-deps', 'bash', '-c', script)]).type == 'system_package_install'


@pytest.mark.parametrize(('install', 'other'), [
    ('true', 'pgrep -f bin/mock | grep -vx "$$" >/dev/null'),
])
def test_review_r7_download_variants_and_filtered_pgrep_are_admitted(install, other):
    validate_static_rules([_cmd('install-x', 'bash', '-c', install)], NO_ACTIVATION, 'linux/aarch64',
                          others=[_cmd('probe-x', 'bash', '-c', other)], system_packages=False)


def test_a_piped_pkill_is_still_corrected():
    script = 'pkill -f bin/mock | cat; exit 0'
    assert _rejected(others=[_cmd('mock-stop', 'bash', '-c', script)]).type == 'self_matching_process_pattern'


def test_the_guidance_says_where_a_test_secret_comes_from():
    from types import SimpleNamespace

    from coordinare.services.environment_admission import LOCAL_CREDENTIAL_GUIDANCE
    from coordinare.services.environment_capabilities import carrier_process_policy

    resources = SimpleNamespace(max_seconds=7200, cpus=8)
    policy = carrier_process_policy(SimpleNamespace(runtime=SimpleNamespace(resources=resources)))
    assert 'openssl rand' in policy['runtime_ordering_guidance']
    assert 'openssl rand' in LOCAL_CREDENTIAL_GUIDANCE['local_credential_invalid']
    assert 'openssl rand' in LOCAL_CREDENTIAL_GUIDANCE['local_credential_in_install']


def test_the_runner_names_the_unresolved_variable_and_never_a_value():
    from coordinare_service_inference.environment.runner import _unresolved_name

    assert _unresolved_name(ValueError('unresolved environment reference: SECRET_KEY_BASE')) == ' ${SECRET_KEY_BASE}'
    assert _unresolved_name(ValueError('some other failure with value s3cr3t')) == ''



def test_the_runtime_credential_correction_agrees_with_the_carrier_guidance():
    """A correction must not forbid what the carrier guidance recommends (e2e-11 bounced between the two)."""
    from coordinare.services.environment_admission import CREDENTIAL_GUIDANCE

    assert 'openssl rand' in CREDENTIAL_GUIDANCE and 'never in a command env field' in CREDENTIAL_GUIDANCE
    assert 'Any other credential comes from the trusted runtime' not in CREDENTIAL_GUIDANCE



def test_the_credential_correction_never_steers_a_generated_secret_into_a_template_reference():
    """Review r1: "Use ${NAME} for every credential" plus a shell-generated secret leads to the rejected self-reference."""
    from coordinare_service_inference.environment.models import Command, validate_shell_locals

    from coordinare.services.environment_admission import CREDENTIAL_GUIDANCE

    assert 'Use ${NAME} for every credential.' not in CREDENTIAL_GUIDANCE
    assert 'as $NAME, never ${NAME}' in CREDENTIAL_GUIDANCE
    for script in ('NAME=$(openssl rand -hex 64) bundle exec rspec', 'NAME=$(openssl rand -hex 64); export NAME; bin/test'):
        validate_shell_locals([Command(id='check-tests', argv=['bash', '-c', script])], [])


@pytest.mark.parametrize('value', ['{cache}/rbenv/shims:{cache}/bin', '${PATH}'])
def test_activation_cannot_replace_the_guest_path(value):
    # Dettmore e2e-23: activation PATH of cache directories only; every install exited 127 without a shell.
    activation = Activation(path=['{cache}/bin'], env={'PATH': value, 'RBENV_ROOT': '{cache}/rbenv'})
    install = [_cmd('tools', 'bash', '-c', 'mkdir -p {cache}/bin')]
    with pytest.raises(PydanticCustomError) as caught:
        validate_static_rules(install, activation, 'linux/aarch64')
    assert caught.value.type == 'activation_path_replaced'
    assert caught.value.context['command_id'] == 'activation'
    validate_static_rules(install, Activation(path=['{cache}/bin'], env={'RBENV_ROOT': '{cache}/rbenv'}), 'linux/aarch64')


def test_activation_path_guidance_names_the_alternative():
    from coordinare.services.environment_admission import GRAPH_GUIDANCE

    assert 'activation.path' in GRAPH_GUIDANCE['activation_path_replaced']


def test_an_unknown_key_gets_removal_guidance_without_being_named():
    from coordinare_service_inference.environment.scope import ScopeAssessmentEnvelope
    from pydantic import ValidationError

    from coordinare.services.environment_admission import UNKNOWN_KEY_GUIDANCE, _validation_feedback

    # Dettmore e2e-24: reviewers added decisions_note beside decisions and never learned which key to drop.
    with pytest.raises(ValidationError) as caught:
        ScopeAssessmentEnvelope.model_validate({'scope_assessment': {
            'verdict': 'accept', 'rationale': 'r', 'sources': ['a'], 'findings': [], 'decisions': [],
            'decisions_review_note': 'private note'}})
    feedback = [item for item in _validation_feedback(caught.value) if item['code'] == 'extra_forbidden']
    assert feedback and feedback[0]['location'][-1] == '*'
    assert feedback[0]['guidance'] == UNKNOWN_KEY_GUIDANCE
    assert 'decisions_review_note' not in str(feedback) and 'private note' not in str(feedback)
