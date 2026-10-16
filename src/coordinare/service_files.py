from __future__ import annotations

import os
import plistlib
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

LABEL = "com.vividynamics.coordinare"
UNIT = "coordinare.service"
RESTART_SECONDS = 5
DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


@dataclass(frozen=True)
class ServiceDefinition:
    config: Path
    env: Path
    command: tuple[str, ...]
    working_directory: Path
    version: str
    pythonpath: str = ""
    env_optional: bool = False

    def metadata(self) -> dict[str, Any]:
        return {
            "config": str(self.config), "env": str(self.env),
            "command": list(self.command), "working_directory": str(self.working_directory),
            "version": self.version, "pythonpath": self.pythonpath,
            "env_optional": self.env_optional,
        }

    @classmethod
    def from_metadata(cls, data: dict[str, Any]) -> ServiceDefinition:
        return cls(
            config=Path(data["config"]), env=Path(data["env"]),
            command=tuple(data["command"]), working_directory=Path(data["working_directory"]),
            version=data["version"], pythonpath=data.get("pythonpath", ""),
            env_optional=data.get("env_optional", False),
        )


def service_path(definition: ServiceDefinition) -> str:
    # Login managers do not inherit the interactive shell's PATH.
    return ":".join(dict.fromkeys([
        str(Path(definition.command[0]).parent), str(Path.home() / ".local/bin"),
        *DEFAULT_PATH.split(":"),
    ]))


def _unit_quote(value: str, *, command: bool = False) -> str:
    if any(character in value for character in "\n\r\0"):
        message = "Service paths and arguments must be single-line values."
        raise ValueError(message)
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command:
        escaped = escaped.replace("$", "$$")
    return '"' + escaped + '"'


def _unit_path(path: Path) -> str:
    # WorkingDirectory and EnvironmentFile each consume a whole, unquoted path.
    # They do not use ExecStart's word parser or shell quoting rules.
    value = str(path)
    if any(character in value for character in "\n\r\0") or value.endswith("\\"):
        message = "Service paths must be single-line values without a trailing backslash."
        raise ValueError(message)
    return value.replace("%", "%%")


def render_unit(definition: ServiceDefinition) -> str:
    command = " ".join(_unit_quote(arg, command=True) for arg in (
        *definition.command, "--config", str(definition.config),
    ))
    env_prefix = "-" if definition.env_optional else ""
    environment = "Environment=" + _unit_quote("PATH=" + service_path(definition)) + "\n"
    if definition.pythonpath:
        environment += "Environment=" + _unit_quote("PYTHONPATH=" + definition.pythonpath) + "\n"
    return (
        "[Unit]\nDescription=Coordinare board orchestrator\n\n"
        "[Service]\nType=exec\n"
        f"WorkingDirectory={_unit_path(definition.working_directory)}\n"
        f"EnvironmentFile={env_prefix}{_unit_path(definition.env)}\n"
        + environment
        + f"ExecStart={command}\nRestart=on-failure\nRestartSec={RESTART_SECONDS}\n\n"
        "[Install]\nWantedBy=default.target\n"
    )


def render_wrapper(definition: ServiceDefinition) -> str:
    command = shlex.join((*definition.command, "--config", str(definition.config)))
    # The env file written by init contains shell-quoted, literal assignments.
    env = shlex.quote(str(definition.env))
    source = f". {env}\n"
    if definition.env_optional:
        source = f"if [ -f {env} ]; then\n  . {env}\nfi\n"
    return f"#!/bin/sh\nset -eu\nset -a\n{source}set +a\nexec {command}\n"


def render_plist(definition: ServiceDefinition, wrapper: Path, log: Path) -> bytes:
    environment = {"PATH": service_path(definition)}
    if definition.pythonpath:
        environment["PYTHONPATH"] = definition.pythonpath
    return plistlib.dumps({
        "Label": LABEL, "ProgramArguments": ["/bin/sh", str(wrapper)],
        "WorkingDirectory": str(definition.working_directory),
        "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": RESTART_SECONDS,
        "StandardOutPath": str(log), "StandardErrorPath": str(log),
        "EnvironmentVariables": environment,
    })


def user_systemd_directory() -> Path:
    value = os.environ.get("XDG_CONFIG_HOME", "")
    root = Path(value) if value and Path(value).is_absolute() else Path.home() / ".config"
    return root / "systemd/user"
