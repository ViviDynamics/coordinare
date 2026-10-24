"""Native guest unless the pinned source explicitly names the other CPU; no model reads these files."""
from __future__ import annotations

import pytest
from coordinare_service_inference.environment.guest_selection import (
    GuestSelection,
    candidate,
    guest_for,
    pinned_requirements,
    read_candidates,
    select,
)

ARM = 'linux/aarch64'
X86 = 'linux/x86_64'


@pytest.mark.parametrize(('path', 'text'), [
    ('Dockerfile', 'FROM --platform=linux/amd64 debian:trixie-slim\n'),
    ('services/api/Dockerfile.prod', 'FROM --platform linux/amd64 node:22\n'),
    ('compose.yaml', 'services:\n  web:\n    platform: linux/amd64\n'),
    ('docker-compose.override.yml', 'services:\n  db:\n    platform: "linux/amd64"\n'),
    ('.github/workflows/ci.yml', 'run: docker buildx build --platform linux/amd64 -t x .\n'),
    ('bin/build', 'docker run --platform=linux/amd64 img\n'),
    ('Makefile', 'pull:\n\tdocker pull --platform linux/amd64 img\n'),
    ('scripts/setup.sh', 'export DOCKER_DEFAULT_PLATFORM=linux/amd64\n'),
])
def test_an_explicit_amd64_platform_in_a_build_run_or_pull_names_the_guest(path, text):
    selection = select([(path, text)])
    assert selection.required == (X86,)
    assert selection.triggers == (path,)
    assert guest_for(ARM, selection.required) == X86


@pytest.mark.parametrize(('path', 'text'), [
    ('Dockerfile', 'FROM --platform=$BUILDPLATFORM debian\n'),  # a variable is not an explicit requirement
    ('Dockerfile', '# FROM --platform=linux/amd64 debian\nFROM debian\n'),  # a comment never counts
    ('compose.yaml', 'services:\n  web:\n    platform: ${PLATFORM}\n'),
    ('scripts/deps.sh', 'pip download --platform manylinux2014_x86_64 pkg\n'),  # pip tags are not Linux platforms
    ('README.md', 'docker run --platform linux/amd64 img\n'),  # documentation is not a candidate file
    ('Dockerfile', 'FROM --platform=linux/arm64 debian\n'),  # the carrier's own CPU requires no guest change
])
def test_a_non_explicit_or_native_mention_selects_nothing_foreign(path, text):
    assert guest_for(ARM, select([(path, text)]).required) == ARM


def test_no_requirement_gets_the_native_guest():
    assert guest_for(ARM, select([('Dockerfile', 'FROM debian\n')]).required) == ARM
    assert guest_for(X86, select([('Dockerfile', 'FROM debian\n')]).required) == X86


def test_an_arm_requirement_on_an_x86_carrier_selects_the_arm_guest():
    assert guest_for(X86, select([('Dockerfile', 'FROM --platform=linux/arm64 debian\n')]).required) == ARM


def test_a_requirement_for_the_carrier_cpu_is_never_a_foreign_trigger():
    selection = select([('Dockerfile', 'FROM --platform=linux/aarch64 debian\n')])
    assert selection == GuestSelection(required=(ARM,), triggers=('Dockerfile',), unsupported=())
    assert guest_for(ARM, selection.required) == ARM


def test_other_linux_cpus_are_recorded_but_do_not_change_the_guest():
    selection = select([('Dockerfile', 'FROM --platform=linux/arm/v7 debian\n')])
    assert selection.required == () and selection.unsupported == ('linux/arm/v7',)
    assert guest_for(ARM, selection.required) == ARM


def test_triggers_are_sorted_paths_and_bounded():
    files = [(f'services/s{index:03}/Dockerfile', 'FROM --platform=linux/amd64 x\n') for index in range(40)]
    selection = select(files)
    assert len(selection.triggers) == 32
    assert selection.triggers == tuple(sorted(selection.triggers))


def test_only_build_run_and_ci_files_are_candidates():
    assert candidate('Dockerfile') and candidate('a/b.Dockerfile') and candidate('compose.ci.yaml')
    assert candidate('.github/workflows/build.yml') and candidate('Makefile') and candidate('mk/tools.mk')
    assert candidate('bin/release') and candidate('scripts/x.sh')
    assert not candidate('docs/guide.md') and not candidate('src/app.py')


def test_pinned_requirements_read_only_candidate_files_inside_the_tree(tmp_path):
    (tmp_path / 'Dockerfile').write_text('FROM --platform=linux/amd64 debian\n')
    (tmp_path / 'docs').mkdir()
    (tmp_path / 'docs' / 'a.md').write_text('--platform linux/amd64\n')
    (tmp_path / 'big.sh').write_text('x' * (300 * 1024) + '\n--platform linux/amd64\n')
    (tmp_path / 'link.sh').symlink_to(tmp_path / 'Dockerfile')
    record = pinned_requirements(tmp_path, ['Dockerfile', 'docs/a.md', 'big.sh', 'link.sh'])
    assert record == {'required': [X86], 'triggers': ['Dockerfile'], 'unsupported': [], 'excluded_publish_steps': [],
                      'unclassified_ci': []}
    assert [path for path, _ in read_candidates(tmp_path, ['Dockerfile', 'link.sh', 'big.sh'])] == ['Dockerfile']
