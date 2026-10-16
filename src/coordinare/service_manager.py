from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import yaml

from coordinare.metrics import _coordinare_version
from coordinare.service_files import (
    LABEL,
    UNIT,
    ServiceDefinition,
    render_plist,
    render_unit,
    render_wrapper,
    user_systemd_directory,
)
from coordinare.user_config import (
    ENV_FILENAME,
    PRIVATE_DIRECTORY_MODE,
    SECRET_MODE,
    read_environment,
    user_config_directory,
    user_data_directory,
)

if TYPE_CHECKING:
    from typing import Any

COMMAND_TIMEOUT_SECONDS = 60
LOG_LINES = 100
DEFAULT_DASHBOARD_PORT = 8090


def _write_file(path: Path, content: bytes) -> None:
    staging = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            staging = Path(handle.name)
            staging.chmod(SECRET_MODE)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        staging.replace(path)
    finally:
        if staging is not None:
            staging.unlink(missing_ok=True)


class ServiceError(ValueError):
    """Credential-free operator guidance for a service operation failure."""


class Runner(Protocol):
    def __call__(self, argv: list[str], *, capture: bool = True) -> subprocess.CompletedProcess[str]: ...


def run_command(argv: list[str], *, capture: bool = True) -> subprocess.CompletedProcess[str]:
    executable = shutil.which(argv[0])
    if executable is None:
        message = f"Required service tool {argv[0]} is unavailable."
        raise ServiceError(message)
    try:
        result = asyncio.run(_execute_command(executable, argv[1:], capture=capture))
    except (OSError, TimeoutError):
        message = f"Service tool {argv[0]} could not complete. Check your login session."
        raise ServiceError(message) from None
    return result


async def _execute_command(executable: str, arguments: list[str], *, capture: bool) -> subprocess.CompletedProcess[str]:
    process = await asyncio.create_subprocess_exec(
        executable, *arguments,
        stdout=asyncio.subprocess.PIPE if capture else None,
        stderr=asyncio.subprocess.PIPE if capture else None,
    )
    try:
        async with asyncio.timeout(COMMAND_TIMEOUT_SECONDS if capture else None):
            stdout, stderr = await process.communicate()
    except (TimeoutError, asyncio.CancelledError):
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    return subprocess.CompletedProcess(
        [executable, *arguments], process.returncode or 0,
        (stdout or b"").decode(errors="replace"), (stderr or b"").decode(errors="replace"),
    )


def dashboard_url(definition: ServiceDefinition) -> str:
    try:
        raw = yaml.safe_load(definition.config.read_text())
        if not isinstance(raw, dict):
            return "unavailable (check config)"
        raw = raw.get("global_config", raw)
        if not isinstance(raw, dict):
            return "unavailable (check config)"
        values = {**read_environment(definition.env), **os.environ}
        host = str(raw.get("dashboard_host", "127.0.0.1"))
        port = str(raw.get("dashboard_port", DEFAULT_DASHBOARD_PORT))
        variable = r"\$\{(\w+)\}|\$(\w+)"
        host = re.sub(variable, lambda match: values.get(match.group(1) or match.group(2), match.group()), host)
        port = re.sub(variable, lambda match: values.get(match.group(1) or match.group(2), match.group()), port)
        # Display only valid host and port syntax, never arbitrary config scalars.
        if not re.fullmatch(r"[A-Za-z0-9_.:\[\]-]+", host) or not port.isdecimal():
            return "unavailable (check config)"
        if host in {str(ipaddress.IPv4Address(0)), "::", "[::]"}:
            host = "127.0.0.1"
        elif ":" in host and not host.startswith("["):
            host = "[" + host + "]"
    except (OSError, ValueError, yaml.YAMLError):
        return "unavailable (check config)"
    return f"http://{host}:{port}"


class ServiceManager:
    def __init__(self, *, system: str | None = None, runner: Runner = run_command) -> None:
        self.system = system or platform.system()
        if self.system not in {"Darwin", "Linux"}:
            message = "User services are supported on macOS and Linux. Use coordinare in the foreground here."
            raise ServiceError(message)
        self.runner = runner
        self.directory = user_data_directory() / "service"
        self.metadata_path = self.directory / "registration.json"
        self.wrapper_path = self.directory / "run.sh"
        self.log_path = self.directory / "coordinare.log"
        self.unit_path = (
            Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
            if self.system == "Darwin" else user_systemd_directory() / UNIT
        )
        self.domain = f"gui/{os.getuid()}"
        self.target = self.domain + "/" + LABEL

    def _call(self, *argv: str, capture: bool = True, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = self.runner(list(argv), capture=capture)
        if check and result.returncode:
            guidance = (
                "Check that a systemd user session is available (systemctl --user status)."
                if self.system == "Linux" else "Run from your macOS graphical login session."
            )
            message = f"Service command {argv[0]} {argv[1]} failed. {guidance}"
            raise ServiceError(message)
        return result

    def _systemctl(self, *argv: str) -> subprocess.CompletedProcess[str]:
        return self._call("systemctl", "--user", *argv)

    def _registered(self) -> bool:
        return self._call("launchctl", "print", self.target, check=False).returncode == 0

    def _require_installation(self) -> None:
        if not self.unit_path.is_file():
            message = "Service is not installed. Run coordinare service install first."
            raise ServiceError(message)

    def install(self, definition: ServiceDefinition) -> None:
        if any(path.is_symlink() for path in (self.unit_path, self.wrapper_path, self.metadata_path)):
            message = "Service registration files cannot be symbolic links. Remove the links and rerun install."
            raise ServiceError(message)
        for path in (definition.config, definition.env):
            if not path.is_file():
                message = "Service config or env file is missing. Run coordinare init first."
                raise ServiceError(message)
        # Render before touching disk, including validation of systemd paths.
        unit = render_plist(definition, self.wrapper_path, self.log_path) if self.system == "Darwin" else render_unit(definition).encode()
        self.directory.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
        self.unit_path.parent.mkdir(parents=True, exist_ok=True)
        if self.system == "Darwin" and self._registered():
            self._call("launchctl", "bootout", self.target)
        _write_file(self.unit_path, unit)
        if self.system == "Darwin":
            _write_file(self.wrapper_path, render_wrapper(definition).encode())
        _write_file(self.metadata_path, (json.dumps(definition.metadata()) + "\n").encode())
        if self.system == "Linux":
            self._systemctl("daemon-reload")
            self._systemctl("enable", UNIT)
            # Restart also starts an inactive service and applies a changed registration.
            self._systemctl("restart", UNIT)
        else:
            self._call("launchctl", "enable", self.target)
            self._call("launchctl", "bootstrap", self.domain, str(self.unit_path))

    def start(self) -> None:
        self._require_installation()
        if self.system == "Linux":
            self._systemctl("start", UNIT)
        elif not self._registered():
            self._call("launchctl", "bootstrap", self.domain, str(self.unit_path))
        else:
            self._call("launchctl", "kickstart", self.target)

    def stop(self) -> None:
        self._require_installation()
        if self.system == "Linux":
            self._systemctl("stop", UNIT)
        elif self._registered():
            # bootout removes the job until start or the next login, preventing KeepAlive respawn.
            self._call("launchctl", "bootout", self.target)

    def restart(self) -> None:
        self._require_installation()
        if self.system == "Linux":
            self._systemctl("restart", UNIT)
        elif self._registered():
            self._call("launchctl", "kickstart", "-k", self.target)
        else:
            self.start()

    def uninstall(self) -> None:
        if not self.unit_path.exists():
            return
        if self.system == "Linux":
            self._systemctl("disable", "--now", UNIT)
        else:
            self.stop()
        self.unit_path.unlink()
        self.wrapper_path.unlink(missing_ok=True)
        self.metadata_path.unlink(missing_ok=True)
        if self.system == "Linux":
            self._systemctl("daemon-reload")

    def _definition(self) -> ServiceDefinition:
        try:
            data: dict[str, Any] = json.loads(self.metadata_path.read_text())
            return ServiceDefinition.from_metadata(data)
        except (OSError, ValueError, KeyError, TypeError):
            message = "Service registration metadata is missing or invalid. Rerun coordinare service install."
            raise ServiceError(message) from None

    def _running_since(self) -> tuple[bool, str]:
        if self.system == "Linux":
            result = self._systemctl("show", UNIT, "--property=ActiveState,ExecMainStartTimestamp")
            properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            running = properties.get("ActiveState") == "active"
            return running, properties.get("ExecMainStartTimestamp") or "unknown"
        result = self._call("launchctl", "print", self.target, check=False)
        match = re.search(r"^\s*pid = (\d+)\s*$", result.stdout, re.MULTILINE)
        if result.returncode or match is None:
            return False, "unknown"
        since = self._call("ps", "-o", "lstart=", "-p", match.group(1), check=False)
        return True, since.stdout.strip() if since.returncode == 0 else "unknown"

    def status(self) -> str:
        if not self.unit_path.exists():
            config = user_config_directory() / "config.yaml"
            definition = ServiceDefinition(
                config=config, env=config.parent / ENV_FILENAME, command=(),
                working_directory=config.parent, version=_coordinare_version(),
            )
            running, since, state = False, "n/a", "not running (not installed)"
        else:
            definition = self._definition()
            running, since = self._running_since()
            state = "running" if running else "not running"
        return (
            f"Coordinare: {state}\nSince: {since if running else 'n/a'}\n"
            f"Version (installed): {definition.version}\nConfig: {definition.config}\n"
            f"Dashboard: {dashboard_url(definition)}\n"
        )

    def logs(self) -> None:
        self._require_installation()
        if self.system == "Linux":
            self._call("journalctl", "--user", "--unit", UNIT, "--lines", str(LOG_LINES), "--follow", capture=False)
        else:
            self.log_path.touch(exist_ok=True)
            self._call("tail", "-n", str(LOG_LINES), "-F", str(self.log_path), capture=False)
