from __future__ import annotations

import argparse
import sys
from pathlib import Path

from coordinare.metrics import _coordinare_version
from coordinare.service_files import ServiceDefinition
from coordinare.service_manager import ServiceError, ServiceManager


def main() -> None:
    """Checkout adapter used by bin/install; installed users use service install."""
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="Install the checkout as a user service")
    parser.add_argument("--env", type=Path, default=root / ".env")
    parser.add_argument("--config", type=Path, default=root / "config.yaml")
    args, extra = parser.parse_known_args()
    definition = ServiceDefinition(
        config=args.config.expanduser().resolve(), env=args.env.expanduser().resolve(),
        command=(sys.executable, "-m", "coordinare", *extra), working_directory=root,
        version=_coordinare_version(), pythonpath=str(root / "src"),
    )
    try:
        manager = ServiceManager()
        manager.install(definition)
    except ServiceError as exc:
        sys.stderr.write(str(exc) + "\n")
        sys.exit(1)
    except (OSError, ValueError):
        sys.stderr.write("Service installation failed. Check checkout config and env file permissions.\n")
        sys.exit(1)
    sys.stdout.write(f"Installed checkout service: {manager.unit_path}\n")


if __name__ == "__main__":
    main()
