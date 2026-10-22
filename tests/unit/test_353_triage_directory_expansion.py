"""Triage: a small directory judged environment is expanded to its files; a larger one gets its file listing
in the correction (throughput amendment, section 4)."""
from __future__ import annotations

import pytest
from coordinare_service_inference.environment.inventory import InventoryEntry

from coordinare.services.environment_scope import _ResponseReferenceError
from coordinare.services.environment_shard_triage import validate_triage
from coordinare.services.environment_shards import Shard

SMALL = ['bin/setup', 'bin/test', 'bin/dev']
LARGE = [f'config/file{index:03}.yml' for index in range(40)]
PATHS = (*SMALL, *LARGE, 'README.md')


def shard() -> Shard:
    return Shard(id='s0', paths=PATHS, readable_bytes=0, depth=1)


def entries(size: int = 100) -> dict[str, InventoryEntry]:
    return {path: InventoryEntry(path=path, size=size, mode='100644') for path in PATHS}


def reply(*verdicts: tuple[str, str]) -> dict:
    return {'listing_triage': {'verdicts': [{'path': path, 'verdict': verdict, 'rationale': 'judged from the listing'}
                                            for path, verdict in verdicts]}}


def test_a_small_directory_judged_environment_becomes_its_files():
    verdicts = validate_triage(reply(('bin', 'environment'), ('config', 'unrelated'), ('README.md', 'environment')),
                               shard=shard(), entries=entries())
    assert 'bin' not in verdicts
    assert {path for path, verdict in verdicts.items() if verdict.verdict == 'environment'} == {*SMALL, 'README.md'}
    assert verdicts['bin/setup'].rationale.startswith('Expanded from directory bin: ')


def test_a_deeper_verdict_inside_an_expanded_directory_is_kept():
    verdicts = validate_triage(reply(('bin', 'environment'), ('bin/dev', 'unrelated'), ('config', 'unrelated'),
                                     ('README.md', 'unsure')), shard=shard(), entries=entries())
    assert verdicts['bin/dev'].verdict == 'unrelated'
    assert verdicts['bin/setup'].verdict == 'environment'


def test_a_large_directory_is_corrected_with_its_file_listing():
    with pytest.raises(_ResponseReferenceError) as caught:
        validate_triage(reply(('bin', 'unsure'), ('config', 'environment'), ('README.md', 'unsure')),
                        shard=shard(), entries=entries())
    [error] = [error for error in caught.value.errors if error['code'] == 'triage_environment_directory']
    assert error['directory_files'] == LARGE and error['more_files'] == 0


def test_a_small_directory_over_the_byte_bound_is_not_expanded():
    with pytest.raises(_ResponseReferenceError):
        validate_triage(reply(('bin', 'environment'), ('config', 'unrelated'), ('README.md', 'unsure')),
                        shard=shard(), entries=entries(size=600 * 1024))


def test_without_sizes_a_directory_is_never_expanded():
    with pytest.raises(_ResponseReferenceError):
        validate_triage(reply(('bin', 'environment'), ('config', 'unrelated'), ('README.md', 'unsure')), shard=shard())
