from __future__ import annotations

import os
import secrets
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

from coordinare import __main__ as cli
from coordinare import docker_setup
from coordinare.doctor import CheckResult, DoctorReport, Status
from coordinare.init_cli import run_init
from coordinare.models.performer_endpoint import PerformerEndpointConfig


def provision(tag: str, directory: Path, runner: docker_setup.DockerRunner, variant: str = "full") -> str:
    """Use real init and Docker; replace only external account/model probes."""
    answers = directory / "answers.yaml"
    answers.write_text(yaml.safe_dump({
        "github_token": "smoke-token", "api_key": "smoke-key", "repository": "example/smoke",
        "project_number": 1, "human_reviewers": ["smoke"], "harness": "codex", "run_mode": "docker",
    }))
    args = cli._build_arg_parser().parse_args([
        "init", "--non-interactive", "--answers", str(answers), "--config-dir", str(directory / "config"),
        "--image-tag", tag, "--image-variant", variant,
    ])
    report = DoctorReport([CheckResult("CI account probes", Status.OK, "GitHub and model credentials are stubbed; Docker provisioning is real.")])
    with (
        patch.dict(os.environ, {"XDG_DATA_HOME": str(directory / "data")}),
        patch("coordinare.docker_setup.run_docker", runner),
        patch("coordinare.docker_setup.smoke_environment", return_value={}),
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


def smoke(tag: str, directory: Path, *, runner: docker_setup.DockerRunner = docker_setup.run_docker, variant: str = "full") -> None:
    # Init owns the shared smoke probe, so CI exercises the complete product path.
    provision(tag, directory, runner, variant)


if __name__ == "__main__":
    if sys.argv[1] == "--local-image":
        # Published base images predate this PR; exercise the checkout's entrypoint.
        docker_setup.smoke_container(PerformerEndpointConfig(
            id="smoke", mode="ephemeral", roles=["implementer"], image=sys.argv[2],
            env={"BACKEND": "codex"}, auth_token=secrets.token_urlsafe(32),
        ))
    else:
        with tempfile.TemporaryDirectory(prefix="coordinare-docker-smoke-") as temporary:
            smoke(sys.argv[1], Path(temporary), variant=sys.argv[2] if len(sys.argv) > 2 else "full")
