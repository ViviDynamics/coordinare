"""nare-native-033: the installer's --repo is a bind mount (-v workspace:/repo), which cannot be moved, so the runner's
hiding of the install-time checkout fell back silently and an editable install resolved in every installer check.
The installer entrypoint now runs on a copy it owns, under scratch, which the runner can move out of reach."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from coordinare_service_inference.environment import execution_source_install as entry


def test_the_installer_runs_on_an_owned_copy_of_the_verified_checkout(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    repo, cache, scratch = tmp_path / 'repo', tmp_path / 'cache', tmp_path / 'scratch'
    for path in (repo, cache, scratch):
        path.mkdir()
    (repo / 'guide').write_text('pinned\n')
    seen: dict[str, Any] = {}
    identity = SimpleNamespace(digest=lambda: 'd' * 64)

    async def checkout_identity(path: Path, **_: Any) -> Any:
        seen['verified'] = path
        return identity

    async def run_manifest(manifest: Any, *, repo: str, **kwargs: Any) -> Any:
        copy = Path(repo)
        seen.update(repo=copy, content=(copy / 'guide').read_text(), scratch=kwargs['scratch'])
        return SimpleNamespace(passed=True, runtime_incomplete=False, model_dump_json=lambda: '{}')

    monkeypatch.setattr(entry, 'checkout_identity', checkout_identity)
    monkeypatch.setattr(entry, 'run_manifest', run_manifest)
    monkeypatch.setattr(entry, 'consume_runtime_controls', lambda **_: (None, None))
    monkeypatch.setattr(entry.EnvironmentManifest, 'model_validate', lambda data: SimpleNamespace(
        runtime=None, source_commit='a' * 40, inputs={}))
    monkeypatch.setattr(entry.ExecutionSourceIdentity, 'model_validate', lambda data: identity)
    monkeypatch.setattr(entry, 'run_cli', lambda coroutine: __import__('asyncio').run(coroutine))
    payload = json.dumps({'manifest': {}, 'execution_source': {}}).encode()
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(payload)))
    monkeypatch.setattr(sys, 'stdout', io.StringIO())
    monkeypatch.setattr(sys, 'argv', ['install', '--repo', str(repo), '--cache', str(cache), '--scratch', str(scratch),
                                      '--install'])
    with pytest.raises(SystemExit) as finished:
        entry.main()
    assert finished.value.code == 0
    assert seen['verified'] == repo
    assert seen['repo'] != repo and scratch in seen['repo'].parents and seen['content'] == 'pinned\n'
    assert not seen['repo'].exists()  # The copy goes with the installer.
    assert (repo / 'guide').read_text() == 'pinned\n'
