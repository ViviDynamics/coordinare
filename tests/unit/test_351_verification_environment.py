"""Clean verifiers receive curated credentials through ephemeral stdin only."""
from __future__ import annotations

import base64
import json
import shlex
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coordinare.models.env_cache import EnvCacheState
from tests.unit.env_manifest_fixture import reviewed_cache


@pytest.mark.asyncio
async def test_clean_environment_pins_secret_source_and_quotes_values(tmp_path: Path) -> None:
    from coordinare.services.environment_verification import verification_environment

    github = SimpleNamespace(get_file_content=AsyncMock(return_value='FIXTURE_DSN="literal $(echo unsafe)"\n'))
    effective = SimpleNamespace(github_org="org", project_name="sym")
    sym = SimpleNamespace(name="sym", test_env=SimpleNamespace(repo_path="test.env", host_path=None),
                          effective_config=lambda _: effective)
    config = SimpleNamespace(global_config=effective, symphonies=[sym])
    cache = reviewed_cache(EnvCacheState(symphony_name="sym", sanitised_name="sym", cache_dir=tmp_path))
    script = await verification_environment({"coordinare_config": config, "github_service": github}, "sym", cache, ())
    github.get_file_content.assert_awaited_once_with("org", "sym", "test.env", ref="a" * 40)
    encoded = shlex.split(script)[1].split("=", 1)[1]
    assert json.loads(base64.b64decode(encoded)) == {"FIXTURE_DSN": "literal $(echo unsafe)"}
    assert "literal" not in script
    assert "literal" not in str(cache.environment_manifest)
