from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from coordinare.metrics import _coordinare_version
from coordinare.service_files import ServiceDefinition
from coordinare.service_manager import ServiceError, ServiceManager
from coordinare.user_config import ENV_FILENAME, user_config_directory

ACTIONS = ("install", "start", "stop", "restart", "status", "logs", "uninstall")


def add_service_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("service", help="Manage start at login on macOS and Linux")
    actions = parser.add_subparsers(dest="service_action", required=True)
    for action in ACTIONS:
        command = actions.add_parser(action, help=action.capitalize() + " the user service")
        if action == "install":
            command.add_argument("--config", type=Path, default=argparse.SUPPRESS, help="Config from coordinare init")


def install_service(
    config: Path | None = None, *, manager: ServiceManager | None = None,
) -> None:
    config = (config or user_config_directory() / "config.yaml").expanduser().resolve()
    # The interpreter's sibling entry point selects this installation even
    # when another Coordinare installation appears earlier on PATH.
    sibling = Path(sys.executable).parent / "coordinare"
    executable = str(sibling) if sibling.is_file() else shutil.which("coordinare")
    if not executable:
        message = "Installed coordinare command was not found. Install coordinare before registering a service."
        raise ServiceError(message)
    definition = ServiceDefinition(
        config=config, env=config.parent / ENV_FILENAME, command=(executable,),
        working_directory=config.parent, version=_coordinare_version(),
    )
    (manager or ServiceManager()).install(definition)


def run_service(args: argparse.Namespace) -> int:
    try:
        manager = ServiceManager()
        action = args.service_action
        if action == "install":
            install_service(args.config, manager=manager)
        elif action == "status":
            sys.stdout.write(manager.status())
            return 0
        else:
            operation = getattr(manager, action)
            operation()
        sys.stdout.write(f"Service {action} complete.\n")
    except KeyboardInterrupt:
        return 0
    except ServiceError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 1
    except (OSError, ValueError):
        # Raw filesystem and manager output can contain credentials.
        sys.stderr.write("Service operation failed. Check config and env files, the installed command, and your login session.\n")
        return 1
    return 0
