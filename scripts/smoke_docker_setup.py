from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

from coordinare import __main__ as cli
from coordinare import docker_setup
from coordinare.doctor import CheckResult, DoctorReport, Status
from coordinare.init_cli import run_init

START_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 1.0
PROBE_TIMEOUT_SECONDS = 10.0
HEALTH_PROBE = "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8088/healthz', timeout=3).read().decode())"


def provision(tag: str, directory: Path, runner: docker_setup.DockerRunner) -> str:
    """Use real init and Docker; replace only external account/model probes."""
    answers = directory / "answers.yaml"
    answers.write_text(yaml.safe_dump({
        "github_token": "smoke-token", "api_key": "smoke-key", "repository": "example/smoke",
        "project_number": 1, "human_reviewers": ["smoke"], "harness": "codex", "run_mode": "docker",
    }))
    args = cli._build_arg_parser().parse_args([
        "init", "--non-interactive", "--answers", str(answers), "--config-dir", str(directory / "config"),
        "--image-tag", tag,
    ])
    report = DoctorReport([CheckResult("CI account probes", Status.OK, "GitHub and model credentials are stubbed; Docker provisioning is real.")])
    with (
        patch.dict(os.environ, {"XDG_DATA_HOME": str(directory / "data")}),
        patch("coordinare.docker_setup.run_docker", runner),
        patch("coordinare.init_probes.detected_defaults", return_value={"harness": "codex"}),
        patch("coordinare.init_probes.verify_token", return_value="smoke"),
        patch("coordinare.init_probes.verify_repository"),
        patch("coordinare.init_probes.verify_board"),
        patch("coordinare.init_cli.run_preflight", AsyncMock(return_value=report)),
    ):
        if run_init(args) != 0:
            raise RuntimeError("Non-interactive Docker init failed")
    raw = yaml.safe_load((directory / "config/config.yaml").read_text())
    return str(raw["performer_endpoints"][0]["image"])


def smoke(tag: str, directory: Path, *, runner: docker_setup.DockerRunner = docker_setup.run_docker) -> None:
    image = provision(tag, directory, runner)
    result = runner([
        "docker", "run", "-d", "--rm", "--entrypoint", "python", image,
        "-m", "performer", "--serve", "--port", "8088",
    ], PROBE_TIMEOUT_SECONDS)
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("Performer container did not start")
    container = result.stdout.strip()
    try:
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            probe = runner(["docker", "exec", container, "python", "-c", HEALTH_PROBE], PROBE_TIMEOUT_SECONDS)
            if probe.returncode == 0:
                sys.stdout.write("Published performer started and answered /healthz without a local image build.\n")
                return
            time.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError("Performer health check timed out")
    finally:
        runner(["docker", "rm", "-f", container], PROBE_TIMEOUT_SECONDS)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="coordinare-docker-smoke-") as temporary:
        smoke(sys.argv[1], Path(temporary))
