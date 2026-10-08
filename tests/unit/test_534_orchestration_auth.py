from __future__ import annotations

import json

import pytest

from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services.http_performer_service import HTTPPerformerService


@pytest.mark.parametrize(
    "backend",
    ["claude_code", "codex", "opencode", "junie", "driver", "hermes", "pi", "openclaw"],
)
@pytest.mark.parametrize("bootstrap", [False, True])
def test_orchestration_credentials_reach_each_leg(monkeypatch, backend, bootstrap):
    refs = {
        leg: {"model": leg, "auth_env": f"TEST_{leg.upper()}_TOKEN"}
        for leg in ("tool", "thinking", "classifier")
    }
    for leg, ref in refs.items():
        monkeypatch.setenv(ref["auth_env"], f"secret-{leg}")
    monkeypatch.setenv("TEST_UNRELATED_TOKEN", "unrelated-secret")
    context = {
        "backend": backend,
        "repo_url": "https://github.com/example/repo",
        "branch": "main",
        "orchestration": {"strategy": "conditional", **refs},
        "test_env_vars": {"TEST_TOOL_TOKEN": "test-env-collision"},
    }
    if bootstrap:
        context.update(job_type="env_bootstrap", symphony_org="example", symphony_repo="repo")
    service = HTTPPerformerService(
        PerformerEndpointConfig(
            id="test",
            image="performer:test",
            mode="persistent",
            endpoint="http://localhost:8080",
            roles=["implementing"],
        ),
    )
    payload = service._build_job_payload(context, None)
    for leg, ref in refs.items():
        assert ref["auth_env"] in payload.secrets
        assert payload.secrets[ref["auth_env"]].get_secret_value() == f"secret-{leg}"
    assert "TEST_UNRELATED_TOKEN" not in payload.secrets
    metadata = json.dumps(payload.metadata)
    assert "secret-tool" not in metadata
    assert "secret-thinking" not in metadata
    assert "secret-classifier" not in metadata
    assert payload.metadata["orchestration"] == context["orchestration"]


@pytest.mark.parametrize(
    "orchestration",
    [None, {}, {"tool": None}, {"tool": {"auth_env": "TEST_MISSING_TOKEN"}}],
)
def test_absent_orchestration_credentials_are_optional(monkeypatch, orchestration):
    monkeypatch.delenv("TEST_MISSING_TOKEN", raising=False)
    service = HTTPPerformerService(
        PerformerEndpointConfig(
            id="test",
            image="performer:test",
            mode="persistent",
            endpoint="http://localhost:8080",
            roles=["implementing"],
        ),
    )
    payload = service._build_job_payload(
        {
            "repo_url": "https://github.com/example/repo",
            "branch": "main",
            "orchestration": orchestration,
        },
        None,
    )
    assert "TEST_MISSING_TOKEN" not in payload.secrets


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_role", ["env_bootstrap", "implementer"])
async def test_daemon_bootstrap_forwards_selected_roles_orchestration(monkeypatch, configured_role):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from coordinare.config import PerformerRoleConfig, PerformersConfig
    from coordinare.daemon import CoordinareDaemon
    from coordinare.models.env_cache import BootstrapJobPayload

    refs = {
        leg: {"model": leg, "auth_env": f"BOOTSTRAP_{leg.upper()}"}
        for leg in ("tool", "thinking", "classifier")
    }
    for leg, ref in refs.items():
        monkeypatch.setenv(ref["auth_env"], f"bootstrap-secret-{leg}")
    orchestration = {"strategy": "conditional", **refs}
    resolve_orchestration = MagicMock(return_value=orchestration)
    daemon = CoordinareDaemon(AsyncMock(), max_cycles=1, sleep_func=AsyncMock())
    daemon._state["config"] = SimpleNamespace(
        performers=PerformersConfig(
            **{configured_role: PerformerRoleConfig(backend="claude_code")},
        ),
        resolve_performer_dispatch_model=lambda role: {},
        resolve_performer_orchestration=resolve_orchestration,
    )
    service = MagicMock()
    service.dispatch_card = AsyncMock(return_value={"status": "error", "reason": "fixture"})
    await daemon._execute_bootstrap_dispatch(
        "bootstrap",
        BootstrapJobPayload(
            symphony_name="sym",
            symphony_org="example",
            symphony_repo="repo",
            cache_mount_path="/devenv/sym",
        ),
        "sym",
        {"bootstrap": service},
        MagicMock(),
    )
    context = service.dispatch_card.call_args.args[0]
    payload = HTTPPerformerService._build_env_bootstrap_payload(object(), context)
    for leg, ref in refs.items():
        assert ref["auth_env"] in payload.secrets
        assert payload.secrets[ref["auth_env"]].get_secret_value() == f"bootstrap-secret-{leg}"
    resolve_orchestration.assert_called_once_with(configured_role)
    assert payload.metadata["orchestration"] == orchestration
