from __future__ import annotations


def build_shell_command(config_path, *, structured: bool = False, log_level: str | None = None) -> list[str]:
    command = ["scripts/run-coordinare.sh", "--config", str(config_path)]
    if log_level is not None:
        command.extend(["--log-level", log_level])
    if structured:
        command.append("--structured-output")
    return command


def build_compose_commands(service: str = "coordinare") -> dict[str, list[str]]:
    return {
        "up": ["docker", "compose", "up", "--build", service],
        "logs": ["docker", "compose", "logs", "-f", service],
        "down": ["docker", "compose", "down"],
    }
