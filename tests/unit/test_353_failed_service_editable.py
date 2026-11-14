"""A service whose lifecycle command ran and did not pass stays editable; one that passed stays fixed.

Dettmore e2e-62: the postgres health command was itself a Ruby syntax error (a begin/rescue with no end). Its service
had started, so the command was frozen, and every repair had to keep it until the attempt held.
"""
from __future__ import annotations

import pytest
from coordinare_service_inference.environment.models import EnvironmentManifest

from coordinare.services.environment_attempt import EnvironmentDiscoveryError
from coordinare.services.environment_obligations import executed_manifests as executed
from coordinare.services.environment_obligations import (
    fixed_command_ids,
    preserve_execution_obligations,
)
from tests.unit.test_353_execution_obligations import contract, history

PHASES = ("init", "start", "health", "stop")


def _with_service() -> EnvironmentManifest:
    data = contract().model_dump()
    data["services"] = [{"id": "backend", "required": True,
                         **{phase: {"id": "backend-" + phase, "argv": ["backend-tool", phase]} for phase in PHASES}}]
    return EnvironmentManifest.model_validate(data)


def _service_ran(manifest: EnvironmentManifest, *, health: str) -> list[dict]:
    """The service started and stopped; its health check ended with ``health``."""
    records = history(manifest, ran=("suite", "backend-init", "backend-start", "backend-health", "backend-stop"))
    for result in records[0]["report"]["results"]:
        if result["id"] == "backend-health":
            result.update(status=health, exit_code=1 if health == "fail" else (None if health == "indeterminate" else 0))
    return records


def _health_corrected(manifest: EnvironmentManifest) -> EnvironmentManifest:
    data = manifest.model_dump()
    data["services"][0]["health"]["argv"] = ["backend-tool", "health", "--corrected"]
    return EnvironmentManifest.model_validate(data)


@pytest.mark.parametrize("health", ["fail", "indeterminate"])
def test_a_service_that_did_not_pass_can_have_its_commands_corrected(health):
    previous = _with_service()
    records = _service_ran(previous, health=health)
    assert not {"backend-" + phase for phase in PHASES} & fixed_command_ids(executed(records, source_commit=previous.source_commit))
    preserve_execution_obligations(_health_corrected(previous), records)


def test_a_service_that_passed_stays_fixed_even_after_a_later_failure():
    previous = _with_service()
    records = [*_service_ran(previous, health="pass"), *_service_ran(previous, health="fail")]
    assert "backend-health" in fixed_command_ids(executed(records, source_commit=previous.source_commit))
    with pytest.raises(EnvironmentDiscoveryError):
        preserve_execution_obligations(_health_corrected(previous), records)
