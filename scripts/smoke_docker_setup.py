from __future__ import annotations

import asyncio
import os
import secrets
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

# Direct script invocation must also find the checkout's shared test fakes.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.docker_setup_fakes import connectivity_fakes

from coordinare import __main__ as cli
from coordinare import docker_setup
from coordinare.doctor import CheckResult, DoctorReport, Status
from coordinare.init_cli import run_init
from coordinare.models.performer_endpoint import PerformerEndpointConfig


def provision(
    tag: str, directory: Path, runner: docker_setup.DockerRunner, variant: str = "full",
    *, github_url: str | None = None, model_url: str | None = None,
) -> str:
    """Use real init and Docker; replace host account discovery with stub answers."""
    answers = directory / "answers.yaml"
    answers.write_text(yaml.safe_dump({
        "github_token": "smoke-token", "api_key": "smoke-key", "repository": "example/smoke",
        "project_number": 1, "human_reviewers": ["smoke"], "harness": "codex", "run_mode": "docker",
        **({"endpoint": model_url} if model_url else {}),
    }))
    if github_url:
        config_dir = directory / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text(yaml.safe_dump({"github_api_url": github_url}))
    args = cli._build_arg_parser().parse_args([
        "init", "--non-interactive", "--answers", str(answers), "--config-dir", str(directory / "config"),
        "--image-tag", tag, "--image-variant", variant,
        *(["--force"] if github_url else []),
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


def smoke(tag: str, directory: Path, *, runner: docker_setup.DockerRunner = docker_setup.run_docker, variant: str = "full") -> None:
    # Init owns the shared smoke probe, so CI exercises the complete product path.
    provision(tag, directory, runner, variant)


async def smoke_with_fakes(arguments: list[str], directory: Path) -> None:
    async with connectivity_fakes(directory) as fakes:
        if arguments[0] == "--local-image":
            # Published base images predate this PR; exercise the checkout's entrypoint.
            endpoint = PerformerEndpointConfig(
                id="smoke", mode="ephemeral", roles=["implementer"], image=arguments[1],
                env={"BACKEND": "codex"}, auth_token=secrets.token_urlsafe(32),
                extra_hosts=["host.docker.internal:host-gateway"],
            )
            await asyncio.to_thread(docker_setup.smoke_container, endpoint, connectivity=fakes.probe_environment)
        else:
            await asyncio.to_thread(
                provision, arguments[0], directory, docker_setup.run_docker,
                arguments[1] if len(arguments) > 1 else "full",
                github_url=fakes.github_url, model_url=fakes.model_url,
            )
        fakes.assert_probed()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="coordinare-docker-smoke-") as temporary:
        asyncio.run(smoke_with_fakes(sys.argv[1:], Path(temporary)))
