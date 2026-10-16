from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import subprocess
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


async def smoke_with_fakes(arguments: list[str], directory: Path) -> str:
    async with connectivity_fakes(directory) as fakes:
        if arguments[0] == "--local-image":
            # Published base images predate this PR; exercise the checkout's entrypoint.
            endpoint = PerformerEndpointConfig(
                id="smoke", mode="ephemeral", roles=["implementer"], image=arguments[1],
                env={"BACKEND": "codex"}, auth_token=secrets.token_urlsafe(32),
                extra_hosts=["host.docker.internal:host-gateway"],
            )
            await asyncio.to_thread(docker_setup.smoke_container, endpoint, connectivity=fakes.probe_environment)
            image = arguments[1]
        else:
            image = await asyncio.to_thread(
                provision, arguments[0], directory, docker_setup.run_docker,
                arguments[1] if len(arguments) > 1 else "full",
                github_url=fakes.github_url, model_url=fakes.model_url,
            )
        fakes.assert_probed()
        return image


# Remove only the bind mount's contents, not the mount point itself. rmtree
# does not follow directory symlinks, and top-level symlinks are unlinked.
CLEANUP_PROBE = """
import shutil
from pathlib import Path

for path in Path('/smoke-cleanup').iterdir():
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)
"""


def cleanup_directory(directory: Path, image: str, *, runner: docker_setup.DockerRunner | None = None) -> None:
    """Recover old images' root-owned files without replacing the smoke outcome."""
    try:
        shutil.rmtree(directory)
        return
    except FileNotFoundError:
        return
    except PermissionError:
        # The performer was already stopped by smoke_container's finally block.
        # Bypass its installer and use only the already selected local image.
        command = [
            "docker", "run", "--rm", "--pull=never", "--network=none", "--user", "0:0",
            "--entrypoint", "python", "-v", f"{directory}:/smoke-cleanup:rw",
            image, "-c", CLEANUP_PROBE,
        ]
        try:
            result = (runner or docker_setup.run_docker)(command, 30.0)
            if result.returncode:
                sys.stderr.write(f"WARNING: container cleanup failed for {directory}: {result.stderr.strip()}\n")
        except (OSError, subprocess.TimeoutExpired) as exc:
            sys.stderr.write(f"WARNING: container cleanup failed for {directory}: {exc}\n")
    except OSError as exc:
        sys.stderr.write(f"WARNING: smoke cleanup left files at {directory}: {exc}\n")
        return
    try:
        shutil.rmtree(directory)
    except FileNotFoundError:
        pass
    except OSError as exc:
        sys.stderr.write(f"WARNING: smoke cleanup left files at {directory}: {exc}\n")


def main(arguments: list[str]) -> None:
    variant = arguments[1] if len(arguments) > 1 else "full"
    image = arguments[1] if arguments[0] == "--local-image" else f"{docker_setup.IMAGE_REPOSITORY}-{variant}:{arguments[0]}"
    # Explicit lifecycle avoids TemporaryDirectory's finalizer retrying an
    # unprivileged deletion after our recovery attempt has reported leftovers.
    directory = Path(tempfile.mkdtemp(prefix="coordinare-docker-smoke-"))
    try:
        image = asyncio.run(smoke_with_fakes(arguments, directory))
    finally:
        cleanup_directory(directory, image)


if __name__ == "__main__":
    main(sys.argv[1:])
