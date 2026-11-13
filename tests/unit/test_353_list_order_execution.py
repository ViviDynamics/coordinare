"""Commands run in the author's list order wherever their prerequisites allow.

Dettmore e2e-70: install-npm was listed after install-gems with no depends_on. The runner ran every command without
prerequisites first, so the asset build ran before Ruby existed and failed with "bundle: not found".
"""
from __future__ import annotations

import pytest
from coordinare_service_inference.environment import runner
from coordinare_service_inference.environment.models import CheckResult, Command

from coordinare.services.environment_discovery_prompts import _MANIFEST_INSTRUCTIONS


def _command(identifier: str, *depends_on: str) -> Command:
    return Command(id=identifier, argv=["true"], depends_on=list(depends_on))


async def _order(monkeypatch, commands: list[Command]) -> list[str]:
    ran: list[str] = []

    async def execute(command, **_):
        ran.append(command.id)
        return CheckResult(id=command.id, status="pass", required=True)

    monkeypatch.setattr(runner, "_execute", execute)
    await runner._commands(commands, roots={}, env={}, digest="d", scope="", results={})
    return ran


@pytest.mark.asyncio
async def test_a_command_without_prerequisites_still_runs_in_its_list_position(monkeypatch):
    commands = [_command("install-system"), _command("install-ruby", "install-system"),
                _command("install-gems", "install-ruby"), _command("install-npm")]
    assert await _order(monkeypatch, commands) == ["install-system", "install-ruby", "install-gems", "install-npm"]


@pytest.mark.asyncio
async def test_declared_prerequisites_still_come_first_whatever_the_list_order(monkeypatch):
    commands = [_command("build", "install-gems"), _command("install-gems")]
    assert await _order(monkeypatch, commands) == ["install-gems", "build"]


def test_authors_are_told_list_order_and_cross_toolchain_prerequisites():
    assert "Commands run in list order where depends_on allows" in _MANIFEST_INSTRUCTIONS
    assert "another toolchain" in _MANIFEST_INSTRUCTIONS
