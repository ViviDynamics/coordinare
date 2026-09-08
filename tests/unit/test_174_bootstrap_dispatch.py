"""174: card-less bootstrap dispatch to the real HTTP payload and Score."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from performer.models import Score

from coordinare.config import PerformerRoleConfig, PerformersConfig
from coordinare.daemon import CoordinareDaemon
from coordinare.models.env_cache import BootstrapJobPayload
from coordinare.services.http_performer_service import HTTPPerformerService


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("timeout", ["1800", "oops"])
async def test_bootstrap_only_uses_its_own_workflow_and_round_trips_flags(enabled, timeout):
    daemon = CoordinareDaemon(AsyncMock(), max_cycles=1, sleep_func=AsyncMock())
    role = PerformerRoleConfig(backend="codex", workflow="env_bootstrap",
                              workflow_env={"ENV_BOOTSTRAP_MAX_REPAIRS": "2", "ENV_BOOTSTRAP_TIMEOUT_SECONDS": timeout}) if enabled else None
    daemon._state["config"] = SimpleNamespace(
        performers=PerformersConfig(env_bootstrap=role,
                                    implementer=PerformerRoleConfig(backend="codex", workflow="implementer")))
    daemon._state["coordinare_config"] = SimpleNamespace(bootstrap_max_seconds=60)
    service = MagicMock()
    service.dispatch_card = AsyncMock(return_value={"status": "error", "reason": "fixture"})
    payload = BootstrapJobPayload(symphony_name="sym", symphony_org="o", symphony_repo="r",
                                  cache_mount_path="/devenv/sym", verify_provided=True,
                                  activate_provided=True)
    await daemon._execute_bootstrap_dispatch("bootstrap", payload, "sym", {"bootstrap": service}, MagicMock())
    context = service.dispatch_card.call_args.args[0]
    # Builder has no dependency on initialized runtime state for this pure transformation.
    wire = HTTPPerformerService._build_env_bootstrap_payload(object(), context)
    score = Score.model_validate({**wire.metadata, "repo_url": str(wire.repo_url),
                                  "branch": wire.branch, "role": wire.role})
    assert score.verify_provided and score.activate_provided
    assert score.workflow == ("env_bootstrap" if enabled else "")
    if enabled:
        assert score.workflow_env == {"ENV_BOOTSTRAP_MAX_REPAIRS": "2", "ENV_BOOTSTRAP_TIMEOUT_SECONDS": "60" if timeout == "1800" else timeout}


async def test_symphony_override_selects_bootstrap_workflow():
    from coordinare.config import CoordinareConfiguration, ProjectConfiguration, SymphonyConfig
    global_config = ProjectConfiguration(project_name="r", github_org="o", github_project_number=1,
                                         github_token="token", human_reviewers=["test"],
                                         performers=PerformersConfig(implementer=PerformerRoleConfig(backend="codex")))
    symphony = SymphonyConfig(name="sym", github_project_number=2,
        overrides={"performers": {"env_bootstrap": {"backend": "codex", "workflow": "env_bootstrap",
                    "workflow_env": {"ENV_BOOTSTRAP_MAX_REPAIRS": "0"}}}})
    daemon = CoordinareDaemon(AsyncMock(), max_cycles=1, sleep_func=AsyncMock())
    daemon._state["config"] = global_config
    daemon._state["coordinare_config"] = CoordinareConfiguration(global_config=global_config, symphonies=[symphony])
    daemon._state["symphony_configs"] = {"sym": symphony}
    service = MagicMock()
    service.dispatch_card = AsyncMock(return_value={"status": "error", "reason": "fixture"})
    payload = BootstrapJobPayload(symphony_name="sym", symphony_org="o", symphony_repo="r", cache_mount_path="/devenv/sym")
    await daemon._execute_bootstrap_dispatch("bootstrap", payload, "sym", {"bootstrap": service}, MagicMock())
    context = service.dispatch_card.call_args.args[0]
    assert context["workflow"] == "env_bootstrap"
    assert context["workflow_env"]["ENV_BOOTSTRAP_MAX_REPAIRS"] == "0"
    assert global_config.performers.env_bootstrap is None
