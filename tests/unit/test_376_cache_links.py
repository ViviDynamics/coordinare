"""nare-native-032: provenance refused the cache twice, one interpreter link per freeze (uvtools/bin/python, then
venv/bin/python, both linking the base image's /usr/local/bin/python3), and each refusal cost a full repair. A cache may
now link into the pinned base image's system directories, which the clean consumer shares; every other escaping link
is named at once."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from coordinare_service_inference.environment.cache_links import (
    base_image_target,
    cache_links,
    escaping_links,
    link_refusal,
)

from coordinare.services.env_provenance import ProvenanceRefusedError
from tests.unit.test_234_review_links import _cache, _freeze


@pytest.mark.parametrize('target', ['/usr/local/bin/python3', '/usr/bin/python3', '/bin/sh', '/lib/x', '/lib64/ld.so'])
def test_a_link_into_the_base_images_system_directories_is_accepted(target: str) -> None:
    assert base_image_target(target) and link_refusal('venv/bin/python', target, {}) is None


@pytest.mark.parametrize('target', ['/tmp/x', '/home/u/x', '/devenv/other/bin/python', '/usr/../tmp/x', '/usr/./x',
                                    '/var/lib/x', '/root/x', '/usrx/bin'])
def test_any_other_absolute_target_still_leaves_the_cache(target: str) -> None:
    assert link_refusal('venv/bin/python', target, {}) == 'points outside the cache'


def test_a_chain_through_a_base_image_link_is_followed_only_when_it_ends_there() -> None:
    links = {'venv/bin/python': 'python3', 'venv/bin/python3': '/usr/local/bin/python3',
             'lib/up': '../venv/bin/python3/../x'}
    assert link_refusal('venv/bin/python', 'python3', links) is None
    assert link_refusal('lib/up', links['lib/up'], links) == 'points outside the cache'


def test_every_escaping_link_is_named_in_one_refusal(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    (cache / 'venv' / 'bin').mkdir(parents=True)
    os.symlink('/usr/local/bin/python3', cache / 'venv' / 'bin' / 'python')  # accepted
    os.symlink('/tmp/build/tool', cache / 'bin' / 'tool')
    os.symlink('../../../repo/src', cache / 'venv' / 'src')
    links = cache_links(cache)
    assert links['venv/bin/python'] == '/usr/local/bin/python3'
    assert [item.rel for item in escaping_links(links)] == ['bin/tool', 'venv/src']
    with pytest.raises(ProvenanceRefusedError) as refused:
        _freeze(cache, mounts)
    assert '2 links leave the cache' in str(refused.value)
    assert 'bin/tool' in str(refused.value) and 'venv/src' in str(refused.value)
    assert 'venv/bin/python ' not in str(refused.value)
    assert 'must copy it into {cache}' in str(refused.value)
    # nare-native-033: the AWS CLI installer wrote absolute links into the cache's own mount path.
    assert 'A link to something inside {cache} is relative' in str(refused.value)


def test_a_cache_whose_only_external_links_name_the_base_image_freezes(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    (cache / 'uvtools' / 'bin').mkdir(parents=True)
    os.symlink('python3', cache / 'uvtools' / 'bin' / 'python')
    os.symlink('/usr/local/bin/python3', cache / 'uvtools' / 'bin' / 'python3')
    _freeze(cache, mounts)


@pytest.mark.asyncio
async def test_the_install_that_leaves_an_escaping_link_fails_as_it_finishes(tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    from typing import Any

    from coordinare_service_inference.environment import runner
    from coordinare_service_inference.environment.models import CheckResult, Command

    cache = tmp_path / 'cache'
    cache.mkdir()

    async def execute(command: Command, **_: Any) -> CheckResult:
        if command.id == 'install-venv':
            (cache / 'venv' / 'bin').mkdir(parents=True)
            os.symlink('/usr/local/bin/python3', cache / 'venv' / 'bin' / 'python')
            os.symlink('/tmp/build/libfoo.so', cache / 'venv' / 'libfoo.so')
        return CheckResult(id=command.id, status='pass', required=True, output='ok', manifest_digest='d' * 64)

    monkeypatch.setattr(runner, '_execute', execute)
    results: dict[str, CheckResult] = {}
    commands = [Command(id='install-tool', argv=['true']),
                Command(id='install-venv', argv=['true'], depends_on=['install-tool'])]
    await runner._commands(commands, roots={}, env={}, digest='d' * 64, scope='', results=results, cache=cache)
    assert results['install-tool'].status == 'pass'
    venv = results['install-venv']
    assert venv.status == 'fail' and 'venv/libfoo.so' in venv.output and 'venv/bin/python' not in venv.output
