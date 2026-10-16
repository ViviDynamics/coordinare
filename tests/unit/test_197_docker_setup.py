from __future__ import annotations

import asyncio
import json
import subprocess
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
    assert all(Path(mount["host_path"]).is_dir() for mount in endpoint["volumes"])
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
    assert "host.docker.internal:host-gateway" in command
    assert not any("private-" in argument for argument in command)
    assert all(call.args[0] != "build" for call in boundary.call_args_list)


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
