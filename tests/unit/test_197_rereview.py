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
from tests.unit.test_252_secret_placeholders import _WALKED, _doc_for

ROOT = Path(__file__).resolve().parents[2]
UNSET = "COORDINARE_197_MISSING_SECRET"


@pytest.mark.parametrize("answer,gh_code,expected", [
    ("2026.10.145", 0, 0), ("", 0, 1), ("null", 0, 1), ("2026.10.145", 7, 1),
])
def test_197_release_script_with_strict_gh_stub(tmp_path, answer, gh_code, expected):
    gh = tmp_path / "gh"
    gh.write_text('''#!/bin/sh
test "$*" = 'release list --exclude-drafts --exclude-pre-releases --limit 1 --json tagName -q .[0].tagName' || exit 64
printf '%s\\n' "$TEST_RELEASE"
exit "$TEST_GH_CODE"
''')
    gh.chmod(0o700)
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/resolve_smoke_release.sh")],
        capture_output=True, text=True, check=False,
        env={**os.environ, "PATH": str(tmp_path) + ":" + os.environ["PATH"],
             "TEST_RELEASE": answer, "TEST_GH_CODE": str(gh_code), "GITHUB_OUTPUT": str(output)},
    )
    assert result.returncode == expected, result.stderr
    if expected == 0:
        assert output.read_text() == "tag=2026.10.145\n"
    else:
        assert "published release" in result.stderr.lower()
        assert not output.exists()


@pytest.mark.parametrize("path", _WALKED, ids=[".".join(p) for p in _WALKED])
def test_197_yaml_loader_rejects_every_unexpanded_secret(tmp_path, monkeypatch, path):
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


@pytest.mark.parametrize("path", _WALKED, ids=[".".join(p) for p in _WALKED])
def test_197_in_memory_validation_refuses_unexpanded_secrets(monkeypatch, path):
    monkeypatch.delenv(UNSET, raising=False)
    result = validate_raw_config({
        "github_org": "example", "github_token": "test-token", "github_project_number": 1,
        "human_reviewers": ["smoke"], **_doc_for(path, "${" + UNSET + "}"),
    })
    assert not result.passed
    assert any(UNSET in error.fix_hint for error in result.errors)


@pytest.mark.parametrize("token", [None, "", "   ", "${COORDINARE_197_MISSING_SECRET}"])
def test_197_ephemeral_refuses_missing_or_unexpanded_auth_before_docker(monkeypatch, token):
    monkeypatch.delenv(UNSET, raising=False)
    runner = AsyncMock()
    monkeypatch.setattr(performer_lifecycle, "_run_docker", runner)
    endpoint = PerformerEndpointConfig(id="smoke", mode="ephemeral", roles=["implementer"],
                                       image="smoke:full", auth_token=token)
    with pytest.raises(performer_lifecycle.ContainerStartError, match="auth_token"):
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
    try:
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
    except PermissionError:
        pytest.skip("Sandbox forbids localhost listeners; this probe runs on GitHub Actions")
