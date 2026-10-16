from __future__ import annotations

import argparse
import json
import shlex
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from coordinare.service_cli import install_service, run_service
from coordinare.service_manager import ServiceManager, run_command
from coordinare.user_config import SECRET_MODE

if TYPE_CHECKING:
    from coordinare.service_files import ServiceDefinition

WAIT_SECONDS = 10
POLL_SECONDS = 0.1
TOKEN = "service-smoke-fixture-value"
FIXTURE = '''from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path
FIXTURE_POLL_SECONDS = 1
config = Path(sys.argv[sys.argv.index("--config") + 1])
assert os.environ["SERVICE_SMOKE_TOKEN"] == "service-smoke-fixture-value"
assert os.environ["SERVICE_SMOKE_QUOTING"] == "space ' quote $ dollar % percent"
config.with_suffix(".received").write_text(json.dumps(str(config)))
while True:
    time.sleep(FIXTURE_POLL_SECONDS)
'''


class SmokeManager(ServiceManager):
    """Keep the real managers and generated files, replacing only the daemon."""

    def __init__(self, fixture: Path) -> None:
        super().__init__(system="Linux")
        self.fixture = fixture

    def install(self, definition: ServiceDefinition) -> None:
        super().install(replace(definition, command=(sys.executable, str(self.fixture))))


def exercise(directory: Path) -> None:
    config = directory / "config.yaml"
    config.write_text("dashboard_host: 127.0.0.1\ndashboard_port: 8090\n")
    env = directory / "coordinare.env"
    env.write_text(
        "SERVICE_SMOKE_TOKEN=" + shlex.quote(TOKEN) + "\n"
        "SERVICE_SMOKE_QUOTING=" + shlex.quote("space ' quote $ dollar % percent") + "\n",
    )
    env.chmod(SECRET_MODE)
    fixture = directory / "fixture.py"
    fixture.write_text(FIXTURE)
    manager = SmokeManager(fixture)
    if manager.unit_path.exists():
        message = "Refusing to replace a preexisting Coordinare service in the smoke test."
        raise RuntimeError(message)
    original = (config.read_bytes(), env.read_bytes())
    try:
        install_service(config, manager=manager)
        assert run_command(["systemctl", "--user", "is-enabled", "coordinare.service"]).stdout.strip() == "enabled"
        deadline = time.monotonic() + WAIT_SECONDS
        received = config.with_suffix(".received")
        while not received.exists() and time.monotonic() < deadline:
            time.sleep(POLL_SECONDS)
        assert received.exists(), "The fixture did not receive its config and environment"
        assert json.loads(received.read_text()) == str(config)
        assert TOKEN not in manager.unit_path.read_text()
        assert TOKEN not in manager.metadata_path.read_text()
        status = manager.status()
        assert "Coordinare: running" in status
        assert str(config) in status
        assert "Since: unknown" not in status
        assert "Version (installed):" in status
        assert "Dashboard: http://127.0.0.1:8090" in status
        assert run_service(argparse.Namespace(service_action="status")) == 0
        manager.stop()
        assert "not running" in manager.status()
        manager.start()
        manager.restart()
    finally:
        assert run_service(argparse.Namespace(service_action="uninstall")) == 0
    assert not manager.unit_path.exists()
    assert not manager.metadata_path.exists()
    assert original == (config.read_bytes(), env.read_bytes())
    assert run_service(argparse.Namespace(service_action="status")) == 0
    sys.stdout.write("Linux user service lifecycle passed.\n")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="coordinare service % $ ' ") as temporary:
        exercise(Path(temporary))


if __name__ == "__main__":
    main()
