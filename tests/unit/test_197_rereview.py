from __future__ import annotations

import asyncio
import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml
from scripts import smoke_docker_setup
from scripts.docker_setup_fakes import ConnectivityFakes

from coordinare import docker_setup
from coordinare.config import ProjectConfiguration
from coordinare.config_validation import validate_config, validate_raw_config
from coordinare.init_files import SetupError
from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services import performer_lifecycle
from coordinare.start_checks import validate_expanded_secrets
from tests.unit.test_252_secret_placeholders import _WALKED, _doc_for
from tests.utils.smoke_release import release_script_env


@pytest.fixture(autouse=True)
def _ample_docker_disk(monkeypatch):
    """These tests fake Docker, so its disk check measured the runner's own temporary directory; the Ubuntu 26
    runners have less than the full image's 10 GiB free there. No test here exercises the disk check."""
    import shutil

    real = shutil.disk_usage
    monkeypatch.setattr(docker_setup.shutil, "disk_usage",
                        lambda path: real(path)._replace(free=64 * 1024 ** 3))

ROOT = Path(__file__).resolve().parents[2]
UNSET = "COORDINARE_197_MISSING_SECRET"


@pytest.mark.parametrize("answer,gh_code,expected", [
    ("2026.10.145", 0, 0), ("", 0, 1), ("null", 0, 1), ("2026.10.145", 7, 1),
])
def test_197_release_script_with_strict_gh_stub(tmp_path, answer, gh_code, expected):
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/resolve_smoke_release.sh")],
        capture_output=True, text=True, check=False,
        env=release_script_env(tmp_path, answer, gh_code=gh_code),
    )
    assert result.returncode == expected, result.stderr
    if expected == 0:
        assert output.read_text() == "tag=2026.10.145\n"
    else:
        assert "published release" in result.stderr.lower()
        assert not output.exists()


@pytest.mark.parametrize("statuses,chosen,skipped", [
    ({"base:2026.10.163": "404"}, "2026.10.160", "base"),
    ({"full:2026.10.163": "404"}, "2026.10.160", "full"),
    ({}, "2026.10.163", None),
    ({"base:2026.10.163": "404", "full:2026.10.160": "404"}, None, "full"),
    ({"full:2026.10.163": "401"}, "2026.10.160", "full"),
    ({"full:2026.10.163": "500"}, "2026.10.160", "full"),
    ({"full:2026.10.163": "transport-error"}, "2026.10.160", "full"),
])
def test_197_release_requires_both_performer_manifests(tmp_path, statuses, chosen, skipped):
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/resolve_smoke_release.sh")],
        capture_output=True, text=True, check=False,
        env=release_script_env(tmp_path, "2026.10.163\n2026.10.160", manifest_statuses=statuses),
    )
    output = tmp_path / "output"
    if chosen:
        assert result.returncode == 0, result.stderr
        assert output.read_text() == f"tag={chosen}\n"
        assert f"Selected {chosen}" in result.stdout
        assert "base and full" in result.stdout
    else:
        assert result.returncode != 0
        assert not output.exists()
        assert "::error::" in result.stderr
        assert "published release" in result.stderr
        assert "base and full" in result.stderr
    if skipped:
        assert "Skipping 2026.10.163" in result.stdout
        assert skipped in result.stdout
        for status in statuses.values():
            reason = "request failed" if status == "transport-error" else f"HTTP {status}"
            assert reason in result.stdout
        if chosen is None:
            assert "Skipping 2026.10.160" in result.stdout
    else:
        assert "Skipping" not in result.stdout


@pytest.mark.parametrize("token_response", ["{}", "not-json", '{"token": ""}', "transport-error"])
def test_197_release_requires_anonymous_registry_token(tmp_path, token_response):
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/resolve_smoke_release.sh")],
        capture_output=True, text=True, check=False,
        env=release_script_env(tmp_path, "2026.10.163", token_response=token_response),
    )
    assert result.returncode != 0
    assert not (tmp_path / "output").exists()
    assert "anonymous GHCR token" in result.stdout
    assert "::error::" in result.stderr


@pytest.mark.parametrize("releases,statuses,chosen", [
    ("invalid\n2026.10.160.1", {}, "2026.10.160.1"),
    (
        "2026.10.163\n2026.10.162\n2026.10.161\n2026.10.160\n2026.10.159",
        {"base:2026.10.163": "404", "base:2026.10.162": "404",
         "base:2026.10.161": "404", "base:2026.10.160": "404"},
        "2026.10.159",
    ),
])
def test_197_release_walks_all_candidates(tmp_path, releases, statuses, chosen):
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/resolve_smoke_release.sh")],
        capture_output=True, text=True, check=False,
        env=release_script_env(tmp_path, releases, manifest_statuses=statuses),
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "output").read_text() == f"tag={chosen}\n"
    for tag in releases.splitlines()[:-1]:
        assert tag in result.stdout
    if releases.startswith("invalid"):
        assert "invalid published release tag" in result.stdout


_PERFORMER_AUTH_PATH = ("performer_endpoints", "[]", "auth_token")
_PERFORMER_AUTH_PATHS = [
    _PERFORMER_AUTH_PATH,
    ("global_config", *_PERFORMER_AUTH_PATH),
    ("symphonies", "[]", "overrides", *_PERFORMER_AUTH_PATH),
]


@pytest.mark.parametrize("path", _PERFORMER_AUTH_PATHS, ids=[".".join(p) for p in _PERFORMER_AUTH_PATHS])
def test_197_yaml_loader_rejects_unexpanded_performer_auth(tmp_path, monkeypatch, path):
    monkeypatch.delenv(UNSET, raising=False)
    monkeypatch.delenv("COORDINARE_GITHUB_TOKEN", raising=False)
    raw = {"github_org": "example", "human_reviewers": ["smoke"],
           "github_token": "test-token", "github_project_number": 1}
    # A partial secret-bearing subtree must be refused before unrelated validation.
    raw.update(_doc_for(path, "${" + UNSET + "}"))
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match=UNSET):
        ProjectConfiguration.from_yaml(config)
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    assert not validate_config(config).passed


@pytest.mark.parametrize("path", _PERFORMER_AUTH_PATHS, ids=[".".join(p) for p in _PERFORMER_AUTH_PATHS])
def test_197_in_memory_validation_refuses_unexpanded_performer_auth(monkeypatch, path):
    monkeypatch.delenv(UNSET, raising=False)
    result = validate_raw_config({
        "github_org": "example", "github_token": "test-token", "github_project_number": 1,
        "human_reviewers": ["smoke"], **_doc_for(path, "${" + UNSET + "}"),
    })
    assert not result.passed
    assert any(UNSET in error.fix_hint for error in result.errors)


@pytest.mark.parametrize("path", [path for path in _WALKED if path != _PERFORMER_AUTH_PATH])
def test_197_config_load_check_leaves_other_secret_fields_unchanged(monkeypatch, path):
    monkeypatch.delenv(UNSET, raising=False)
    for prefix in [(), ("global_config",), ("symphonies", "[]", "overrides")]:
        validate_expanded_secrets(_doc_for((*prefix, *path), "${" + UNSET + "}"))


def test_197_unset_optional_dashboard_auth_placeholder_loads_as_origin_main(tmp_path, monkeypatch):
    monkeypatch.delenv(UNSET, raising=False)
    monkeypatch.delenv("COORDINARE_DASHBOARD_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(Path, "cwd", lambda: tmp_path)
    placeholder = "${" + UNSET + "}"
    raw = {"github_org": "example", "github_token": "test-token", "github_project_number": 1,
           "project_name": "legacy", "human_reviewers": ["smoke"], "dashboard_auth_token": placeholder}
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(raw))

    loaded = ProjectConfiguration.from_yaml(config)
    assert loaded.dashboard_auth_token.get_secret_value() == placeholder
    result = validate_config(config)
    assert result.passed, result.errors
    result = validate_raw_config(raw)
    assert result.passed, result.errors


@pytest.mark.parametrize("token", ["${COORDINARE_197_MISSING_SECRET}", "prefix-${COORDINARE_197_MISSING_SECRET}"])
def test_197_ephemeral_refuses_unexpanded_auth_before_docker(monkeypatch, token):
    monkeypatch.delenv(UNSET, raising=False)
    runner = AsyncMock()
    monkeypatch.setattr(performer_lifecycle, "_run_docker", runner)
    endpoint = PerformerEndpointConfig(id="smoke", mode="ephemeral", roles=["implementer"],
                                       image="smoke:full", auth_token=token)
    with pytest.raises(performer_lifecycle.ContainerStartError, match="auth_token.*" + UNSET):
        asyncio.run(performer_lifecycle.start_ephemeral(endpoint))
    runner.assert_not_called()


@pytest.mark.parametrize("eventually_ready", [True, False])
def test_197_exec_timeout_retries_until_probe_deadline(monkeypatch, eventually_ready):
    commands = []
    elapsed = [0.0]
    def runner(command, timeout):
        commands.append(command)
        if command[1] == "exec":
            attempts = sum(c[1] == "exec" for c in commands)
            if attempts == 1 or not eventually_ready:
                elapsed[0] += timeout
                raise subprocess.TimeoutExpired(command, timeout)
        return subprocess.CompletedProcess(command, 0, "ready", "")
    monkeypatch.setattr(docker_setup.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(docker_setup.time, "sleep", lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))
    monkeypatch.setattr(docker_setup, "START_TIMEOUT_SECONDS", 25)
    endpoint = PerformerEndpointConfig(id="smoke", mode="ephemeral", roles=["implementer"],
                                       image="smoke:full", auth_token="test-token")
    if eventually_ready:
        docker_setup.smoke_container(endpoint, runner=runner)
    else:
        with pytest.raises(SetupError, match="smoke timed out"):
            docker_setup.smoke_container(endpoint, runner=runner)
        assert elapsed[0] <= 25
    assert sum(c[1] == "exec" for c in commands) >= 2
    assert commands[-1][1] == "rm"


def test_197_ci_provision_passes_real_connectivity_environment(tmp_path):
    observed = {}
    def runner(command, timeout):
        if command[1] == "run":
            from coordinare.user_config import read_environment
            observed.update(read_environment(Path(command[command.index("--env-file") + 1])))
        if command[1] == "image":
            return subprocess.CompletedProcess(command, 0, '[{"Config":{"Entrypoint":["/entrypoint.sh"]}}]', "")
        if command[1] == "info":
            return subprocess.CompletedProcess(command, 0, "{}", "")
        return subprocess.CompletedProcess(command, 0, "container", "")
    smoke_docker_setup.provision("2026.10.145", tmp_path, runner)
    assert observed["COORDINARE_SMOKE_GITHUB_TOKEN"] == "smoke-token"
    assert observed["COORDINARE_SMOKE_MODEL_TOKEN"] == "smoke-key"


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["base", "full"])
@pytest.mark.parametrize("omitted", [None, "github", "model"])
async def test_197_ci_lanes_run_probe_and_require_both_fakes(tmp_path, monkeypatch, variant, omitted):
    import io
    import urllib.request

    from coordinare.user_config import read_environment

    fakes = ConnectivityFakes(github_url="http://host.docker.internal:19001",
                              model_url="http://host.docker.internal:19002/v1")
    @asynccontextmanager
    async def servers(directory):
        yield fakes
    monkeypatch.setattr(smoke_docker_setup, "connectivity_fakes", servers)
    values = {}
    def urlopen(request, **kwargs):
        assert request.get_header("Authorization") == "Bearer " + values["PERFORMER_AUTH_TOKEN"] or request.full_url.startswith("http://host.docker.internal:")
        if request.full_url.endswith("/healthz"):
            return io.BytesIO(b"{}")
        if request.full_url.endswith("/status"):
            return io.BytesIO(b'{"availability":"idle","auth_enabled":true,"capabilities":{"backends":["codex"]}}')
        if request.full_url == fakes.github_url + "/user":
            assert request.get_header("Authorization") == "Bearer smoke-token"
            fakes.github_requests.append(request.full_url)
        else:
            assert request.full_url == fakes.model_url + "/models"
            assert request.get_header("Authorization") == "Bearer smoke-key"
            fakes.model_requests.append(request.full_url)
        return io.BytesIO(b"{}")
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    def runner(command, timeout):
        if command[1] == "run":
            assert "host.docker.internal:host-gateway" in command
            values.update(read_environment(Path(command[command.index("--env-file") + 1])))
        if command[1] == "exec":
            assert command[-1] == docker_setup.SMOKE_PROBE
            if omitted:
                values.pop("COORDINARE_SMOKE_GITHUB_TOKEN" if omitted == "github" else "COORDINARE_SMOKE_MODEL_URL")
            from unittest.mock import patch
            with patch.dict(os.environ, values, clear=True):
                exec(command[-1], {})
        if command[1] == "image":
            return subprocess.CompletedProcess(command, 0, '[{"Config":{"Entrypoint":["/entrypoint.sh"]}}]', "")
        return subprocess.CompletedProcess(command, 0, "{}" if command[1] == "info" else "ready", "")
    monkeypatch.setattr(docker_setup, "run_docker", runner)
    args = ["--local-image", "smoke:base"] if variant == "base" else ["2026.10.145", "full"]
    if omitted:
        with pytest.raises(RuntimeError, match="both authenticated"):
            await smoke_docker_setup.smoke_with_fakes(args, tmp_path)
    else:
        await smoke_docker_setup.smoke_with_fakes(args, tmp_path)
        assert fakes.github_requests and fakes.model_requests


@pytest.mark.asyncio
async def test_197_ci_real_probe_reaches_local_fakes(tmp_path, monkeypatch):
    # The CI harness must run the actual urllib probe against authenticated HTTP fakes.
    fake_servers = smoke_docker_setup.connectivity_fakes
    async with fake_servers(tmp_path, bind_host="127.0.0.1", gateway_host="127.0.0.1") as fakes:
        monkeypatch.setenv("BACKEND", "codex")
        monkeypatch.setenv("PERFORMER_AUTH_TOKEN", "performer-token")
        for key, value in fakes.probe_environment.items():
            monkeypatch.setenv(key, value)
        # Only performer health/status are in-process; external probes use real sockets.
        import io
        import urllib.request
        original = urllib.request.urlopen
        def urlopen(request, **kwargs):
            if request.full_url.endswith("/healthz"):
                return io.BytesIO(b"{}")
            if request.full_url == "http://127.0.0.1:8088/status":
                return io.BytesIO(b'{"availability":"idle","auth_enabled":true,"capabilities":{"backends":["codex"]}}')
            return original(request, **kwargs)
        monkeypatch.setattr(urllib.request, "urlopen", urlopen)
        await asyncio.to_thread(exec, docker_setup.SMOKE_PROBE, {})
        fakes.assert_probed()
        for url in (fakes.github_url + "/user", fakes.model_url + "/models"):
            with pytest.raises(urllib.error.HTTPError) as caught:
                await asyncio.to_thread(original, url, timeout=3)
            assert caught.value.code == 401
