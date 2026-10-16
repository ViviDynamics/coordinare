from __future__ import annotations

import asyncio
import errno
import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import yaml
from scripts import smoke_docker_setup

from coordinare import docker_setup, launch_preflight
from coordinare.config import ProjectConfiguration
from coordinare.config_validation import validate_config
from coordinare.doctor import CheckResult, DoctorReport, Status, check_performer_versions
from coordinare.init_files import SetupError
from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services import performer_lifecycle
from tests.unit.test_195_init import answers_file, invoke

RELEASE = "2026.10.84"
REPOSITORY = "ghcr.io/vividynamics/coordinare-performer-full"
TAGGED = REPOSITORY + ":" + RELEASE
DIGEST = "sha256:" + "a" * 64
PINNED = TAGGED + "@" + DIGEST
FREE_BYTES = 100 * 1024**3
CLEANUP_IMAGE = "ghcr.io/vividynamics/coordinare-performer-full:2026.10.145"
CLEANUP_PINNED = CLEANUP_IMAGE + "@sha256:" + "a" * 64


@pytest.mark.parametrize("operation,stage,error,cause,fix", [
    ("info", "docker_runtime", "network is unreachable", "network unreachable", "network"),
    ("pull", "image_pull", "no space left on device", "ENOSPC", "docker system df"),
    ("image", "image_inspect", "network is unreachable", "network unreachable", "network"),
    ("run", "container_smoke", "no space left on device", "ENOSPC", "Docker filesystem"),
    ("exec", "container_smoke", "permission denied while trying to connect to the Docker daemon socket", "Docker socket permission denied", "Docker access"),
    ("run", "container_smoke", "pull access denied", "registry access denied", "docker login ghcr.io"),
    ("run", "container_smoke", "manifest unknown", "manifest unknown", "--image-tag"),
    ("run", "container_smoke", "network unreachable", "network unreachable", "network"),
])
def test_docker_failures_report_stage_known_cause_and_fix_without_stderr(
    setup: Path, docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], operation: str, stage: str, error: str, cause: str, fix: str,
) -> None:
    monkeypatch.setattr(docker_setup, "START_TIMEOUT_SECONDS", 0.01)
    docker.fail_operation = operation
    docker.error = error + " ghp_private_token https://user:password@example.test"
    assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path, run_mode="docker"))) == 1
    output = capsys.readouterr()
    line = output.err.splitlines()[0]
    assert f"Setup failed at {stage}:" in line and cause in line
    assert "Fix:" in output.err and fix in output.err
    assert "ghp_private_token" not in output.out + output.err
    assert "user:password" not in output.out + output.err
    assert not setup.exists()
    if operation in {"run", "exec"}:
        assert docker.calls[-1][1:3] == ["rm", "-f"]


@pytest.mark.parametrize("error", [
    OSError(errno.ENOSPC, "ghp_private_token https://user:password@example.test"),
    ValueError("no space left on device ghp_private_token https://user:password@example.test"),
    subprocess.CalledProcessError(1, ["docker", "run"], stderr="no space left on device ghp_private_token"),
])
def test_docker_smoke_exceptions_preserve_disk_cause(
    setup: Path, docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], error: Exception,
) -> None:
    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if command[1] == "run":
            raise error
        return docker(command, timeout)

    monkeypatch.setattr(docker_setup, "run_docker", runner)
    assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path, run_mode="docker"))) == 1
    output = capsys.readouterr()
    assert "Setup failed at container_smoke:" in output.err
    assert "ENOSPC" in output.err.splitlines()[0]
    assert "Docker filesystem" in output.err and "docker system df" in output.err
    assert "ghp_private_token" not in output.out + output.err
    assert "user:password" not in output.out + output.err
    assert not setup.exists()


def test_disk_preflight_names_stage_and_fix(
    setup: Path, docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(docker_setup.shutil, "disk_usage", lambda path: MagicMock(free=0))
    assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path, run_mode="docker"))) == 1
    output = capsys.readouterr()
    assert "Setup failed at docker_disk:" in output.err
    assert "Insufficient disk space" in output.err
    assert "Fix:" in output.err and "docker system df" in output.err
    assert not setup.exists()


@pytest.mark.parametrize("error", ["HTTP Error 403: Forbidden", "HTTP Error 401: Unauthorized"])
def test_smoke_api_denial_is_not_reported_as_registry_denial(
    setup: Path, docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], error: str,
) -> None:
    monkeypatch.setattr(docker_setup, "START_TIMEOUT_SECONDS", 0.01)
    docker.fail_operation, docker.error = "exec", error
    assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path, run_mode="docker"))) == 1
    output = capsys.readouterr()
    assert "Setup failed at container_smoke:" in output.err
    assert "smoke timed out" in output.err
    assert "registry access denied" not in output.err and "docker login" not in output.err


class FakeDocker:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail_operation = ""
        self.error = ""
        self.exception: Exception | None = None
        self.digests = [REPOSITORY + "@" + DIGEST]
        self.entrypoint = ["/entrypoint.sh"]

    def __call__(self, command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        assert timeout > 0
        assert command[0] == "docker"
        if self.exception:
            raise self.exception
        operation = command[1]
        if operation == self.fail_operation:
            return subprocess.CompletedProcess(command, 1, "", self.error)
        output = "{}"
        if operation == "image":
            output = json.dumps([{"RepoDigests": self.digests, "Config": {"Entrypoint": self.entrypoint}}])
        return subprocess.CompletedProcess(command, 0, output, "")


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> FakeDocker:
    runner = FakeDocker()
    monkeypatch.setattr(docker_setup, "run_docker", runner)
    monkeypatch.setattr(docker_setup, "_coordinare_version", lambda: RELEASE)
    monkeypatch.setattr(docker_setup.shutil, "disk_usage", lambda path: MagicMock(free=FREE_BYTES))
    return runner


@pytest.mark.parametrize("exception,expected", [
    (FileNotFoundError(), "Install Docker"),
    (PermissionError(), "permission"),
    (subprocess.TimeoutExpired("docker", 1), "timed out"),
])
def test_docker_unavailable_has_actionable_error(docker: FakeDocker, tmp_path: Path, exception: Exception, expected: str) -> None:
    docker.exception = exception
    with pytest.raises(SetupError, match=expected):
        docker_setup.prepare_image("full", None, tmp_path, runner=docker)
    assert len(docker.calls) == 1


@pytest.mark.parametrize("error,expected", [
    ("Cannot connect to the Docker daemon", "Start Docker"),
    ("permission denied while trying to connect", "permission"),
])
def test_daemon_and_permission_failures_stop_before_pull(docker: FakeDocker, tmp_path: Path, error: str, expected: str) -> None:
    docker.fail_operation, docker.error = "info", error
    with pytest.raises(SetupError, match=expected):
        docker_setup.prepare_image("full", None, tmp_path, runner=docker)
    assert not any(command[1] == "pull" for command in docker.calls)


def test_disk_space_checked_before_pull(docker: FakeDocker, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(docker_setup.shutil, "disk_usage", lambda path: MagicMock(free=0))
    with pytest.raises(SetupError, match="disk space"):
        docker_setup.prepare_image("full", None, tmp_path, runner=docker)
    assert not any(command[1] == "pull" for command in docker.calls)


@pytest.mark.parametrize("error,expected", [
    ("manifest unknown", "No published image"),
    ("denied: requested access", "docker login ghcr.io"),
    ("network timeout private-token", "network"),
    ("no space left on device", "disk space"),
])
def test_failed_pull_gives_fix_without_raw_stderr(docker: FakeDocker, tmp_path: Path, error: str, expected: str) -> None:
    docker.fail_operation, docker.error = "pull", error
    with pytest.raises(SetupError, match=expected) as exc:
        docker_setup.prepare_image("full", None, tmp_path, runner=docker)
    assert "private-token" not in str(exc.value)
    assert not any(command[1] == "image" for command in docker.calls)


def test_release_tag_is_pulled_and_inspected_then_digest_pinned(docker: FakeDocker, tmp_path: Path) -> None:
    assert docker_setup.prepare_image("full", None, tmp_path, runner=docker) == PINNED
    assert docker.calls == [["docker", "info", "--format", "{{json .}}"], ["docker", "pull", TAGGED], ["docker", "image", "inspect", TAGGED]]


@pytest.mark.parametrize("digests", [[], ["other.example/image@" + DIGEST]])
def test_tag_retained_when_matching_digest_unavailable(docker: FakeDocker, tmp_path: Path, digests: list[str]) -> None:
    docker.digests = digests
    assert docker_setup.prepare_image("full", None, tmp_path, runner=docker) == TAGGED


@pytest.mark.parametrize("version", ["0.0.0.dev0", "2026.10.84.dev1", "2026.10.84+local", "2026.10.84rc1"])
def test_unpublished_dev_version_requires_explicit_tag(docker: FakeDocker, monkeypatch: pytest.MonkeyPatch, version: str, tmp_path: Path) -> None:
    monkeypatch.setattr(docker_setup, "_coordinare_version", lambda: version)
    with pytest.raises(SetupError, match="--image-tag") as exc:
        docker_setup.prepare_image("full", None, tmp_path, runner=docker)
    assert "published image" in str(exc.value)
    assert docker.calls == []
    assert docker_setup.prepare_image("full", RELEASE, tmp_path, runner=docker) == PINNED


@pytest.mark.parametrize("tag", ["latest", "", "--help", "bad/tag", "foo@sha256:123"])
def test_unsafe_or_floating_tag_rejected(docker: FakeDocker, tmp_path: Path, tag: str) -> None:
    with pytest.raises(SetupError, match="tag"):
        docker_setup.prepare_image("full", tag, tmp_path, runner=docker)
    assert docker.calls == []


def test_base_variant_and_missing_entrypoint(docker: FakeDocker, tmp_path: Path) -> None:
    docker.digests = []
    base = "ghcr.io/vividynamics/coordinare-performer-base:" + RELEASE
    assert docker_setup.prepare_image("base", None, tmp_path, runner=docker) == base
    assert ["docker", "pull", base] in docker.calls
    docker.entrypoint = ["python", "-m", "performer"]
    with pytest.raises(SetupError, match="full"):
        docker_setup.prepare_image("base", None, tmp_path, runner=docker)


def test_inspect_failure_does_not_report_success(docker: FakeDocker, tmp_path: Path) -> None:
    docker.fail_operation = "image"
    with pytest.raises(SetupError, match="inspect"):
        docker_setup.prepare_image("full", None, tmp_path, runner=docker)


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, docker: FakeDocker) -> Path:
    for key in ("COORDINARE_GITHUB_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("coordinare.init_probes.read_command", AsyncMock(return_value=""))
    monkeypatch.setattr("coordinare.init_probes.check_token_and_repo", lambda *a, **k: [])
    monkeypatch.setattr("coordinare.init_probes.check_github", AsyncMock(return_value=[]))
    monkeypatch.setattr("coordinare.init_probes.httpx.get", MagicMock(return_value=httpx.Response(200, json={"login": "operator"})))
    monkeypatch.setattr("coordinare.init_cli.run_preflight", AsyncMock(return_value=DoctorReport([CheckResult("setup", Status.OK, "ready")])) )
    return tmp_path / "config/coordinare/config.yaml"


def test_noninteractive_init_writes_container_wiring_and_no_secrets(setup: Path, docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    answers = answers_file(tmp_path, run_mode="docker")
    assert invoke(monkeypatch, "--non-interactive", "--answers", str(answers)) == 0
    raw = yaml.safe_load(setup.read_text())
    assert raw["performer_image"] == PINNED
    endpoint = raw["performer_endpoints"][0]
    assert endpoint["image"] == PINNED and endpoint["mode"] == "ephemeral"
    assert endpoint["secret_sources"] == {"init_payload": True, "env": False, "creds_file": False}
    assert endpoint["extra_hosts"] == ["host.docker.internal:host-gateway"]
    assert {mount["container_path"] for mount in endpoint["volumes"]} == {"/workspace", "/artifacts"}
    assert raw["env_cache_root"] == str(tmp_path / "data/coordinare/env-caches")
    assert "OPENAI_API_KEY" in raw["env_passthrough"]
    assert validate_config(setup).passed
    config = ProjectConfiguration.from_yaml(setup)
    assert config.performer_endpoints[0].image == PINNED
    assert config.agent_executable == ""
    assert all(Path(mount["host_path"]).is_dir() for mount in endpoint["volumes"] if "host_path" in mount)
    assert "volume_name" in endpoint["volumes"][0]
    assert not (tmp_path / "data/coordinare/workspaces").exists()
    assert Path(raw["env_cache_root"]).is_dir()
    output = capsys.readouterr()
    assert "inspect" in output.out.lower()
    assert "private-token" not in setup.read_text() + output.out + output.err
    assert "private-api-key" not in setup.read_text() + output.out + output.err


def test_cli_tag_override_and_upgrade_repin(setup: Path, docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    answers = answers_file(tmp_path, run_mode="docker")
    monkeypatch.setattr(docker_setup, "_coordinare_version", lambda: "0.0.0.dev0")
    assert invoke(monkeypatch, "--answers", str(answers)) == 1
    assert not setup.exists()
    assert invoke(monkeypatch, "--answers", str(answers), "--image-tag", RELEASE) == 0
    upgraded = "2026.10.85"
    monkeypatch.setattr(docker_setup, "_coordinare_version", lambda: upgraded)
    assert invoke(monkeypatch, "--answers", str(answers), "--force") == 0
    assert ":" + upgraded + "@" in yaml.safe_load(setup.read_text())["performer_endpoints"][0]["image"]


def test_docker_failure_keeps_existing_files(setup: Path, docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    answers = answers_file(tmp_path, run_mode="docker")
    assert invoke(monkeypatch, "--answers", str(answers)) == 0
    original = setup.read_bytes()
    docker.fail_operation = "pull"
    assert invoke(monkeypatch, "--answers", str(answers), "--force") == 1
    assert setup.read_bytes() == original
    assert not setup.with_suffix(".yaml.bak").exists()


def test_containers_need_runtime_api_key(setup: Path, docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path, run_mode="docker", api_key=""))) == 1
    assert "API key" in capsys.readouterr().err
    assert not setup.exists()


@pytest.mark.parametrize("image,status", [(PINNED, Status.OK), (REPOSITORY + ":old@" + DIGEST, Status.WARN), (REPOSITORY + ":latest", Status.WARN)])
def test_doctor_reports_image_version_mismatch(monkeypatch: pytest.MonkeyPatch, image: str, status: Status) -> None:
    monkeypatch.setattr("coordinare.doctor._coordinare_version", lambda: RELEASE)
    config = ProjectConfiguration(preset="containers", harness="codex", github_org="example", github_token="test-token", github_repo="demo", human_reviewers=["operator"], performer_image=image)
    results = check_performer_versions(config)
    assert results and results[0].status is status
    if status is Status.WARN:
        assert "init" in str(results[0].fix)


def test_solo_preset_has_no_version_warning() -> None:
    assert check_performer_versions(ProjectConfiguration(github_org="example", github_token="test-token", human_reviewers=["operator"])) == []


def test_preset_keeps_local_image_without_init() -> None:
    from coordinare.presets import expand_preset

    raw = expand_preset({"preset": "containers", "harness": "codex", "github_repo": "demo"})
    assert raw["performer_endpoints"][0]["image"] == "coordinare-performer:full"


def test_ci_smoke_provisions_before_starting_and_cleans_up(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, docker: FakeDocker) -> None:
    smoke_docker_setup.smoke(RELEASE, tmp_path, runner=docker)
    operations = [command[1] for command in docker.calls]
    assert operations.index("pull") < operations.index("run") < operations.index("exec") < operations.index("rm")
    command = next(command for command in docker.calls if command[1] == "run")
    assert command[-1] == PINNED and "--entrypoint" not in command


def test_ci_workflow_uses_published_image_without_registry_login() -> None:
    root = Path(__file__).resolve().parents[2]
    text = (root / ".github/workflows/docker_setup.yml").read_text()
    assert "scripts/smoke_docker_setup.py" in text
    assert "docker/login-action" not in text
    assert "--local-image coordinare-setup:base" in text


def test_ci_provision_runs_real_init_with_an_injected_docker_boundary(docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinare.user_config import read_environment

    monkeypatch.chdir(tmp_path)
    assert smoke_docker_setup.provision(RELEASE, tmp_path, docker) == PINNED
    assert any(command[1] == "pull" for command in docker.calls)
    for key, value in read_environment(tmp_path / "config/coordinare.env").items():
        monkeypatch.setenv(key, value)
    assert validate_config(tmp_path / "config/config.yaml").passed


def test_generated_config_reaches_ephemeral_dispatch_without_building(setup: Path, docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path, run_mode="docker"))) == 0
    config = ProjectConfiguration.from_yaml(setup)
    boundary = AsyncMock(side_effect=[(0, "container-id", ""), (0, "127.0.0.1:49160", "")])
    monkeypatch.setattr(performer_lifecycle, "_run_docker", boundary)
    started = asyncio.run(performer_lifecycle.start_ephemeral(config.performer_endpoints[0]))
    assert started.container_id == "container-id"
    command = boundary.call_args_list[0].args
    assert command[0] == "run" and command[-1] == PINNED
    assert "PERFORMER_SECRET_SOURCE_ENV=0" in command
    assert "PERFORMER_SECRET_SOURCE_CREDS_FILE=0" in command
    assert "TMPDIR=/workspace" in command
    assert "NODE_COMPILE_CACHE=/var/cache/node-compile-cache" in command
    assert "host.docker.internal:host-gateway" in command
    assert not any("private-" in argument for argument in command)
    assert all(call.args[0] != "build" for call in boundary.call_args_list)


def test_init_smoke_keeps_node_compile_cache_off_workspace_mount(docker: FakeDocker, tmp_path: Path) -> None:
    environments: list[str] = []

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if command[1] == "run":
            environments.append(Path(command[command.index("--env-file") + 1]).read_text())
        return docker(command, timeout)

    smoke_docker_setup.provision(RELEASE, tmp_path, runner)
    assert len(environments) == 1
    assert "TMPDIR=/workspace\n" in environments[0]
    assert "NODE_COMPILE_CACHE=/var/cache/node-compile-cache\n" in environments[0]


@pytest.mark.parametrize("previous,expected", [(None, "/var/cache/node-compile-cache"), ("/opt/cache/node", "/opt/cache/node")])
def test_force_init_updates_node_cache_default_and_preserves_overrides(setup: Path, docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, previous: str | None, expected: str) -> None:
    answers = answers_file(tmp_path, run_mode="docker")
    assert invoke(monkeypatch, "--answers", str(answers)) == 0
    raw = yaml.safe_load(setup.read_text())
    env = raw["performer_endpoints"][0]["env"]
    env.pop("NODE_COMPILE_CACHE")
    if previous is not None:
        env["NODE_COMPILE_CACHE"] = previous
    setup.write_text(yaml.safe_dump(raw))
    assert invoke(monkeypatch, "--answers", str(answers), "--force") == 0
    assert yaml.safe_load(setup.read_text())["performer_endpoints"][0]["env"]["NODE_COMPILE_CACHE"] == expected


@pytest.mark.asyncio
async def test_docker_runner_decodes_output_without_invoking_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    process = MagicMock(returncode=0)
    process.communicate = AsyncMock(return_value=(b"info", b""))
    create = AsyncMock(return_value=process)
    monkeypatch.setattr(docker_setup.asyncio, "create_subprocess_exec", create)
    result = await docker_setup._docker_process(["docker", "info"], 1)
    assert result.returncode == 0 and result.stdout == "info"
    assert create.call_args.args == ("docker", "info")


@pytest.mark.asyncio
async def test_docker_runner_kills_and_reaps_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    process = MagicMock(returncode=0)
    process.communicate = AsyncMock(side_effect=TimeoutError)
    process.wait = AsyncMock()
    monkeypatch.setattr(docker_setup.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(subprocess.TimeoutExpired):
        await docker_setup._docker_process(["docker", "pull", TAGGED], 1)
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()


@pytest.mark.parametrize("metadata", ["not json", "[]", '[{"Config": {}}]'])
def test_bad_image_metadata_is_rejected(docker: FakeDocker, tmp_path: Path, metadata: str) -> None:
    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        if command[1] == "image":
            return subprocess.CompletedProcess(command, 0, metadata, "")
        return docker(command, timeout)

    with pytest.raises(SetupError, match="inspect"):
        docker_setup.prepare_image("full", None, tmp_path, runner=runner)


def test_smoke_failure_still_removes_started_container(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(docker_setup.time, "monotonic", MagicMock(side_effect=[0, docker_setup.START_TIMEOUT_SECONDS + 1]))
    runner = MagicMock(return_value=subprocess.CompletedProcess([], 0, "container-id", ""))
    endpoint = PerformerEndpointConfig(id="starter", mode="ephemeral", roles=["implementer"], image=PINNED, env={"BACKEND": "codex"})
    with pytest.raises(SetupError, match="timed out"):
        docker_setup.smoke_container(endpoint, runner=runner)
    command = runner.call_args_list[0].args[0]
    name = command[command.index("--name") + 1]
    assert runner.call_args.args[0] == ["docker", "rm", "-f", name]


def test_switching_back_to_local_removes_generated_container_transport(setup: Path, docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    answers = answers_file(tmp_path, run_mode="docker")
    assert invoke(monkeypatch, "--answers", str(answers)) == 0
    monkeypatch.setattr("coordinare.init_probes.verify_harness", lambda answers: None)
    assert invoke(monkeypatch, "--answers", str(answers), "--force", "--run-mode", "local") == 0
    config = ProjectConfiguration.from_yaml(setup)
    assert config.preset == "solo" and config.agent_executable
    assert not config.performer_endpoints


def test_switching_local_back_to_docker_restores_ephemeral_endpoint(setup: Path, docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    answers = answers_file(tmp_path, run_mode="docker")
    assert invoke(monkeypatch, "--answers", str(answers)) == 0
    monkeypatch.setattr("coordinare.init_probes.verify_harness", lambda answers: None)
    assert invoke(monkeypatch, "--answers", str(answers), "--force", "--run-mode", "local") == 0
    assert invoke(monkeypatch, "--answers", str(answers), "--force", "--run-mode", "docker") == 0
    config = ProjectConfiguration.from_yaml(setup)
    assert config.performer_endpoints[0].mode == "ephemeral"
    assert config.performer_endpoints[0].image == PINNED


def test_generated_config_passes_doctor_with_external_boundaries_stubbed(setup: Path, docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path, run_mode="docker"))) == 0
    config = ProjectConfiguration.from_yaml(setup)
    monkeypatch.setattr("coordinare.doctor._coordinare_version", lambda: RELEASE)
    monkeypatch.setattr(launch_preflight, "check_port", lambda config: CheckResult("port", Status.OK, "available"))
    monkeypatch.setattr(launch_preflight, "check_github", AsyncMock(return_value=[CheckResult("GitHub", Status.OK, "reachable")]))
    monkeypatch.setattr(launch_preflight, "check_native_models", lambda config: [CheckResult("model", Status.OK, "reachable")])
    monkeypatch.setattr(launch_preflight.shutil, "which", lambda name: "/usr/bin/" + name)
    boundary = MagicMock(return_value=subprocess.CompletedProcess([], 0, "image metadata", ""))
    monkeypatch.setattr(launch_preflight.subprocess, "run", boundary)
    raw = yaml.safe_load(setup.read_text())
    report = asyncio.run(launch_preflight.run_preflight(config, raw=raw, explicit=True))
    assert report.ok
    assert any(result.name == "performer version starter" and result.status is Status.OK for result in report.results)
    assert any(call.args[0][-1] == PINNED for call in boundary.call_args_list if "inspect" in call.args[0])


def test_base_image_has_harness_installer_and_user_binary_path() -> None:
    root = Path(__file__).resolve().parents[2]
    text = (root / "agent/performer/Dockerfile.base").read_text()
    assert "COPY agent/performer/entrypoint.sh /entrypoint.sh" in text
    assert 'ENTRYPOINT ["/entrypoint.sh"]' in text
    assert 'ENV PATH="/root/.local/bin:${PATH}"' in text


def test_keyless_selfhosted_endpoint_is_usable_in_docker_mode(setup: Path, docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    answers = answers_file(tmp_path, run_mode="docker", api_key="", endpoint="http://model.example:4000/v1")
    assert invoke(monkeypatch, "--answers", str(answers)) == 0
    config = ProjectConfiguration.from_yaml(setup)
    assert config.endpoints[0].auth_env is None
    assert config.performer_endpoints[0].image == PINNED


def test_cleanup_removes_tree_without_docker_when_host_can_delete(tmp_path: Path) -> None:
    directory = tmp_path / "smoke"
    directory.mkdir()
    (directory / "config.yaml").write_text("test")
    runner = MagicMock(side_effect=AssertionError("Docker is unnecessary"))
    smoke_docker_setup.cleanup_directory(directory, CLEANUP_IMAGE, runner=runner)
    assert not directory.exists()


def test_cleanup_recovers_container_owned_files_without_following_symlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "smoke with spaces"
    cache = directory / "data/coordinare/workspaces/node-compile-cache/v22"
    cache.mkdir(parents=True)
    (cache / "76499fec").write_text("cache")
    outside = tmp_path / "keep"
    outside.mkdir()
    (outside / "important").write_text("keep")
    (directory / "link").symlink_to(outside, target_is_directory=True)
    rmtree = shutil.rmtree

    def host_cleanup(path: Path) -> None:
        if cache.exists():
            raise PermissionError(13, "denied", str(cache))
        rmtree(path)

    monkeypatch.setattr(smoke_docker_setup.shutil, "rmtree", host_cleanup)

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        assert command[:3] == ["docker", "run", "--rm"]
        assert command[command.index("--user") + 1] == "0:0"
        assert "--pull=never" in command and "--network=none" in command
        assert command[command.index("--entrypoint") + 1] == "python"
        assert command[command.index("-v") + 1] == f"{directory}:/smoke-cleanup:rw"
        assert command[command.index("-c") - 1] == CLEANUP_PINNED
        assert 0 < timeout <= 30
        # Execute the actual recovery program locally, substituting the bind mount.
        result = subprocess.run(
            [sys.executable, "-c", command[-1].replace("/smoke-cleanup", str(directory))],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert not list(directory.iterdir())
        return result

    smoke_docker_setup.cleanup_directory(directory, CLEANUP_PINNED, runner=runner)
    assert not directory.exists()
    assert (outside / "important").read_text() == "keep"


@pytest.mark.parametrize("failure", [
    subprocess.CompletedProcess([], 1, "", "permission denied"),
    FileNotFoundError("docker"),
    subprocess.TimeoutExpired("docker", 30),
])
def test_cleanup_reports_leftovers_when_recovery_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: object) -> None:
    directory = tmp_path / "smoke"
    directory.mkdir()
    cache = directory / "node-compile-cache"
    cache.mkdir()
    monkeypatch.setattr(smoke_docker_setup.shutil, "rmtree", MagicMock(side_effect=PermissionError(13, "denied", str(cache))))
    runner = MagicMock(side_effect=failure) if isinstance(failure, Exception) else MagicMock(return_value=failure)
    smoke_docker_setup.cleanup_directory(directory, CLEANUP_IMAGE, runner=runner)
    output = capsys.readouterr().err
    assert "WARNING" in output and str(directory) in output and "node-compile-cache" in output
    assert cache.exists()


@pytest.mark.parametrize("arguments,image", [
    (["--local-image", "coordinare-setup:base"], "coordinare-setup:base"),
    (["2026.10.145", "base"], "ghcr.io/vividynamics/coordinare-performer-base:2026.10.145"),
    (["2026.10.145", "full"], CLEANUP_IMAGE),
])
@pytest.mark.parametrize("fails", [False, True])
def test_main_cleanup_preserves_smoke_outcome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arguments: list[str], image: str, fails: bool) -> None:
    directory = tmp_path / "smoke"
    directory.mkdir()
    monkeypatch.setattr(smoke_docker_setup.tempfile, "mkdtemp", lambda **kwargs: str(directory))
    monkeypatch.setattr(smoke_docker_setup.shutil, "rmtree", MagicMock(side_effect=PermissionError(13, "denied", str(directory))))
    commands = []

    def runner(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 1, "", "cleanup failed")

    async def smoke_with_fakes(args: list[str], path: Path) -> str:
        assert args == arguments and path == directory
        if fails:
            raise RuntimeError("smoke failed")
        return CLEANUP_PINNED

    monkeypatch.setattr(smoke_docker_setup, "smoke_with_fakes", smoke_with_fakes)
    monkeypatch.setattr(smoke_docker_setup.docker_setup, "run_docker", runner)
    if fails:
        with pytest.raises(RuntimeError, match="smoke failed"):
            smoke_docker_setup.main(arguments)
    else:
        smoke_docker_setup.main(arguments)
    from coordinare.user_config import workspace_volume_name

    assert commands.pop(0) == ["docker", "volume", "rm", workspace_volume_name(directory / "config")]
    assert len(commands) == 1
    assert commands[0][commands[0].index("-c") - 1] == (image if fails else CLEANUP_PINNED)
    assert str(directory) in capsys.readouterr().err


def test_force_migrates_workspace_bind_and_preserves_other_settings(setup: Path, docker: FakeDocker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    answers = answers_file(tmp_path, run_mode="docker")
    assert invoke(monkeypatch, "--answers", str(answers)) == 0
    raw = yaml.safe_load(setup.read_text())
    endpoint = raw["performer_endpoints"][0]
    old_mount = {"host_path": str(tmp_path / "old-workspaces"), "container_path": "/workspace", "mode": "rw"}
    endpoint["volumes"][0] = old_mount
    endpoint["volumes"].append({"host_path": str(tmp_path / "custom"), "container_path": "/custom", "mode": "ro"})
    endpoint["env"]["CUSTOM"] = "keep"
    expected_other = endpoint["volumes"][1:]
    setup.write_text(yaml.safe_dump(raw))
    # Merely loading an old configuration never migrates it.
    assert ProjectConfiguration.from_yaml(setup).performer_endpoints[0].volumes[0].host_path == Path(old_mount["host_path"])
    assert invoke(monkeypatch, "--answers", str(answers), "--force") == 0
    migrated = yaml.safe_load(setup.read_text())["performer_endpoints"][0]
    assert "volume_name" in migrated["volumes"][0]
    assert "host_path" not in migrated["volumes"][0]
    assert migrated["volumes"][1:] == expected_other
    assert migrated["env"]["CUSTOM"] == "keep"
    backup = yaml.safe_load(setup.with_suffix(".yaml.bak").read_text())
    assert backup["performer_endpoints"][0]["volumes"][0] == old_mount
    volume = migrated["volumes"][0]
    assert invoke(monkeypatch, "--answers", str(answers), "--force") == 0
    assert yaml.safe_load(setup.read_text())["performer_endpoints"][0]["volumes"][0] == volume
    runs = [call for call in docker.calls if call[1] == "run"]
    assert f'{volume["volume_name"]}:/workspace:rw' in runs[-1]
