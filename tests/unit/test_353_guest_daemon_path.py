"""Guest selection reads the pinned bytes from the packed execution source, the same archive the daemon stage receives."""
from __future__ import annotations

import hashlib
import time

import pytest
from coordinare_service_inference.environment.execution_source import (
    ExecutionSourceError,
    SourceEntry,
)
from coordinare_service_inference.environment.guest_selection import guest_for, pinned_requirements

from coordinare.services.environment_source_provider import write_execution_bundle

COMMIT = 'a' * 40
ARM = 'linux/aarch64'
X86 = 'linux/x86_64'
WORKFLOW = '.github/workflows/ci.yml'


def pack(tmp_path, files: dict[str, bytes], links: dict[str, str] | None = None):
    """A real ExecutionSourceBundle, written by the provider's own packing code from git-style blob objects."""
    objects = tmp_path / 'objects'
    objects.mkdir()
    entries = []
    for path, data in files.items():
        object_id = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        (objects / object_id).write_bytes(data)
        entries.append(SourceEntry(path=path, mode='100644', object_id=object_id, size=len(data)))
    for path, target in (links or {}).items():
        raw = target.encode()
        object_id = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        (objects / object_id).write_bytes(raw)
        entries.append(SourceEntry(path=path, mode='120000', object_id=object_id, size=len(raw)))
    return write_execution_bundle(COMMIT, entries, objects, tmp_path / 'payload.tar.gz', deadline=time.monotonic() + 60)


def publish_workflow(command: str) -> bytes:
    body = ''.join(f'            {line}\n' for line in command.splitlines())
    return (f'on: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n'
            f'      - name: Publish amd64\n        uses: nick-fields/retry@v4\n        with:\n          command: |\n{body}'
            f'          max_attempts: 3\n').encode()


def daemon_stage(bundle):
    """The daemon's guest stage: the pinned requirement record, then the guest for the carrier."""
    paths = sorted(entry for entry in bundle_paths(bundle))
    record = pinned_requirements(bundle.payload_path, paths, expected=bundle.identity)
    return record, guest_for(ARM, record['required'])


def bundle_paths(bundle):
    from coordinare_service_inference.environment.execution_source import bundle_inventory
    return bundle_inventory(bundle.payload_path, expected=bundle.identity)


def test_a_compose_amd64_service_selects_the_x86_guest_from_the_packed_source(tmp_path):
    bundle = pack(tmp_path, {'compose.full.yaml': b'services:\n  api:\n    platform: linux/amd64\n',
                             'README.md': b'docs\n'})
    record, guest = daemon_stage(bundle)
    assert record['required'] == [X86] and record['triggers'] == ['compose.full.yaml']
    assert guest == X86


def test_a_publish_only_workflow_selects_the_native_guest_and_names_the_excluded_step(tmp_path):
    bundle = pack(tmp_path, {WORKFLOW: publish_workflow('docker buildx build --platform linux/amd64 --push .')})
    record, guest = daemon_stage(bundle)
    assert guest == ARM and record['required'] == []
    assert record['excluded_publish_steps'] == [f'{WORKFLOW} build/Publish amd64']


def test_a_dockerfile_that_names_the_amd64_base_selects_x86(tmp_path):
    bundle = pack(tmp_path, {'guest-image/Dockerfile': b'FROM --platform=linux/amd64 debian\n'})
    assert daemon_stage(bundle)[1] == X86


def test_candidate_files_that_cannot_be_read_hold_selection_instead_of_defaulting_native(tmp_path):
    bundle = pack(tmp_path, {'Dockerfile': b'x' * (300 * 1024) + b'\nFROM --platform=linux/amd64 debian\n'})
    with pytest.raises(ExecutionSourceError) as caught:
        daemon_stage(bundle)
    assert caught.value.code == 'guest_selection_unreadable'


def test_a_candidate_that_is_only_a_symlink_holds_selection(tmp_path):
    bundle = pack(tmp_path, {'README.md': b'docs\n'}, links={'Dockerfile': 'README.md'})
    with pytest.raises(ExecutionSourceError) as caught:
        daemon_stage(bundle)
    assert caught.value.code == 'guest_selection_unreadable'


def test_no_candidate_files_is_a_native_selection_not_an_error(tmp_path):
    bundle = pack(tmp_path, {'README.md': b'docs\n'})
    assert daemon_stage(bundle) == ({'required': [], 'triggers': [], 'unsupported': [], 'excluded_publish_steps': [],
                                     'unclassified_ci': []}, ARM)


def test_the_reader_refuses_a_bundle_for_another_commit(tmp_path):
    bundle = pack(tmp_path, {'Dockerfile': b'FROM debian\n'})
    other = bundle.identity.model_copy(update={'source_commit': 'b' * 40})
    with pytest.raises(ExecutionSourceError):
        pinned_requirements(bundle.payload_path, ['Dockerfile'], expected=other)
