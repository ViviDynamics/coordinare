"""Live performer connectivity never becomes clean verification evidence."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from performer import environment_runtime
from performer.models import Stand

from .test_353_environment_runtime import private_score


@pytest.mark.asyncio
@pytest.mark.parametrize('install', [False, True])
async def test_performer_selects_explicit_runtime_purpose(tmp_path, monkeypatch, install):
    import time

    score, manifest, _ = private_score(tmp_path)
    if install:
        score.role = 'env_bootstrap'
        score.environment_attempt_expires_at = time.time()+60
    open_runtime = AsyncMock(return_value=SimpleNamespace())
    monkeypatch.setattr(environment_runtime, 'open_runtime', open_runtime)
    await environment_runtime.open_score_runtime(score, Stand(path=tmp_path, branch='main'), manifest, tmp_path)
    kwargs = open_runtime.await_args.kwargs
    assert kwargs['purpose'] == ('installer' if install else 'live_consumer')
    assert kwargs['install'] is install
    assert (kwargs['artifacts'] is None) is install
