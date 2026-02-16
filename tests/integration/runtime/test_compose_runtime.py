from __future__ import annotations

from pathlib import Path

from tests.integration.runtime.test_runtime_helpers import build_compose_commands


def test_compose_file_contains_coordinare_service() -> None:
    compose = Path("docker-compose.yml").read_text()

    assert "services:" in compose
    assert "coordinare:" in compose
    assert "COORDINARE_RUN_MODE: compose" in compose


def test_compose_command_shape() -> None:
    commands = build_compose_commands()

    assert commands["up"] == ["docker", "compose", "up", "--build", "coordinare"]
    assert commands["logs"] == ["docker", "compose", "logs", "-f", "coordinare"]
    assert commands["down"] == ["docker", "compose", "down"]
