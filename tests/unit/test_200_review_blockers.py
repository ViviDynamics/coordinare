from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import yaml
from gql.transport.exceptions import TransportConnectionFailed, TransportServerError

import coordinare.launch_preflight as preflight
from coordinare import __main__ as cli
from coordinare.auth import AppAuth
from coordinare.config import ProjectConfiguration
from coordinare.dashboard import DashboardStore
from coordinare.doctor import CheckResult, DoctorReport, Status, check_endpoints
from coordinare.first_launch import running_summary
from coordinare.graph.state import SymphonyRuntimeState
from coordinare.services.github import PermanentGitHubError, TransientGitHubError
from tests.unit.startup_helpers import run_preflight_without_listeners

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def launch_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ProjectConfiguration:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINARE_GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return ProjectConfiguration(
        preset="solo", harness="codex", github_org="example", github_repo="demo",
        github_project_number=1, human_reviewers=["operator"],
        state_file_path=tmp_path / "state.json",
    )


@pytest.fixture
def github_world(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    client = MagicMock()
    client.__enter__.return_value = client
    client.get.return_value = httpx.Response(
        httpx.codes.OK, headers={"X-OAuth-Scopes": "repo, project, read:org"},
        json={"permissions": {"push": True}},
    )
    github = MagicMock()
    github.initialize = AsyncMock()
    github.aclose = AsyncMock()
    github.field_cache = {"status_option_ids": {
        "ready": "todo", "in progress": "doing", "in review": "review", "done": "done",
    }}
    monkeypatch.setattr(preflight.httpx, "Client", MagicMock(return_value=client))
    monkeypatch.setattr(preflight, "GitHubService", MagicMock(return_value=github))
    return client, github


@pytest.fixture
def isolated_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "check_directories", lambda config: [])
    monkeypatch.setattr(preflight, "check_port", lambda config: CheckResult("port", Status.OK, "free"))
    monkeypatch.setattr(preflight, "run_checks", lambda config: DoctorReport())
    monkeypatch.setattr(preflight, "check_harnesses", AsyncMock(return_value=[]))
    monkeypatch.setattr(preflight, "check_native_models", lambda config: [])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [httpx.ConnectError("closed port"), httpx.ReadTimeout("timeout")])
async def test_200_network_warning_reaches_runtime(
    launch_config: ProjectConfiguration, github_world: tuple, isolated_preflight: None,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, failure: Exception,
) -> None:
    client, _ = github_world
    client.get.side_effect = failure
    store = MagicMock(start=AsyncMock(), close=AsyncMock())
    runtime = AsyncMock()
    monkeypatch.setattr(cli, "EventStore", MagicMock(return_value=store))
    run_preflight_without_listeners(monkeypatch, runtime)
    await cli._run(launch_config)
    runtime.assert_awaited_once()
    assert "WARN" in capsys.readouterr().out
    assert client.get.call_count > 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN, httpx.codes.NOT_FOUND])
async def test_200_deterministic_github_failure_refuses_runtime(
    launch_config: ProjectConfiguration, github_world: tuple, isolated_preflight: None,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, status: int,
) -> None:
    client, _ = github_world
    identity = client.get.return_value
    client.get.side_effect = [identity, httpx.Response(status)] if status == httpx.codes.NOT_FOUND else None
    if status != httpx.codes.NOT_FOUND:
        client.get.return_value = httpx.Response(status)
    runtime = AsyncMock()
    run_preflight_without_listeners(monkeypatch, runtime)
    with pytest.raises(cli.RuntimeExecutionError) as exc:
        await cli._run(launch_config)
    assert exc.value.phase == "startup"
    assert exc.value.step == "preflight"
    runtime.assert_not_awaited()
    expected = "GitHub repository" if status == httpx.codes.NOT_FOUND else "GitHub token"
    assert "[FAIL] " + expected in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", [None, "solo"])
async def test_200_missing_blocked_is_compatible_with_full_configs(
    launch_config: ProjectConfiguration, github_world: tuple, isolated_preflight: None,
    preset: str | None,
) -> None:
    config = launch_config.model_copy(update={"preset": preset})
    report = await preflight.run_preflight(config)
    columns = next(result for result in report.results if result.name == "board columns")
    assert columns.status is (Status.FAIL if preset else Status.WARN)
    assert report.ok is (preset is None)


@pytest.mark.asyncio
async def test_200_full_config_scope_warning_and_explicit_repo_probe(
    launch_config: ProjectConfiguration, github_world: tuple, isolated_preflight: None,
) -> None:
    client, _ = github_world
    client.get.return_value = httpx.Response(httpx.codes.OK, headers={"X-OAuth-Scopes": "repo"})
    config = launch_config.model_copy(update={"preset": None})
    report = await preflight.run_preflight(config)
    assert report.ok
    assert any(result.name == "GitHub scopes" and result.status is Status.WARN for result in report.results)
    assert not any("/repos/" in call.args[0] for call in client.get.call_args_list)
    client.get.reset_mock()
    report = await preflight.run_preflight(config, explicit=True)
    assert not report.ok
    assert any("/repos/" in call.args[0] for call in client.get.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    TimeoutError(), TransientGitHubError("server unavailable"), TransportConnectionFailed("offline"),
])
@pytest.mark.parametrize("stage", ["credential", "board"])
async def test_200_async_github_transients_retry_and_warn(
    launch_config: ProjectConfiguration, github_world: tuple, monkeypatch: pytest.MonkeyPatch,
    failure: Exception, stage: str,
) -> None:
    _, github = github_world
    operation = AsyncMock(side_effect=failure)
    if stage == "credential":
        monkeypatch.setattr(preflight, "build_auth", MagicMock(return_value=MagicMock(get_token=operation)))
    else:
        github.initialize = operation
    results = await preflight.check_github(launch_config)
    assert results[-1].status is Status.WARN
    assert operation.await_count > 1
    if stage == "board":
        github.aclose.assert_awaited_once()


@pytest.mark.parametrize("status", [httpx.codes.TOO_MANY_REQUESTS, httpx.codes.SERVICE_UNAVAILABLE])
def test_200_native_http_transients_retry_and_warn(
    launch_config: ProjectConfiguration, monkeypatch: pytest.MonkeyPatch, status: int,
) -> None:
    probe = MagicMock(return_value=httpx.Response(status))
    monkeypatch.setattr(preflight.httpx, "get", probe)
    results = preflight.check_native_models(launch_config)
    assert results[0].status is Status.WARN
    assert probe.call_count > 1


def test_200_retry_can_recover(launch_config: ProjectConfiguration, monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [httpx.ReadTimeout("timeout"), httpx.Response(httpx.codes.OK)]
    probe = MagicMock(side_effect=responses)
    monkeypatch.setattr(preflight.httpx, "get", probe)
    assert preflight.check_native_models(launch_config)[0].status is Status.OK
    assert probe.call_count == len(responses)


def test_200_self_hosted_connection_failure_warns(
    launch_config: ProjectConfiguration, monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint = launch_config.endpoints[0].model_copy(update={"kind": "ollama", "base_url": "http://localhost:1"})
    config = launch_config.model_copy(update={"endpoints": [endpoint]})
    probe = MagicMock(side_effect=httpx.ConnectError("closed port"))
    monkeypatch.setattr(preflight.httpx, "get", probe)
    assert check_endpoints(config)[0].status is Status.WARN
    assert probe.call_count > 1


@pytest.mark.parametrize("preset", ["solo", "containers"])
@pytest.mark.parametrize("harness", ["codex", "claude_code", "opencode"])
def test_200_config_view_matches_runtime_for_all_roles(
    launch_config: ProjectConfiguration, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture, preset: str, harness: str,
) -> None:
    raw = {"preset": preset, "harness": harness, "github_org": "example", "github_repo": "demo",
           "github_project_number": 1, "human_reviewers": ["operator"],
           "performers": {"reviewer": {}, "env_bootstrap": {}}}
    path = tmp_path / "show.yaml"
    path.write_text(yaml.safe_dump(raw))
    monkeypatch.setattr("sys.argv", ["coordinare", "config", "--config", str(path)])
    cli.main()
    shown = yaml.safe_load(capsys.readouterr().out)
    config = ProjectConfiguration(**raw)
    assert config.performers.implementer.backend == harness
    for role in ("implementer", "reviewer", "env_bootstrap"):
        assert shown["performers"][role]["backend"] == config.performers.resolved_role(role).backend


def test_200_endpoint_override_names_linked_catalogs(launch_config: ProjectConfiguration) -> None:
    raw = {"preset": "solo", "harness": "codex", "github_org": "example", "github_repo": "demo",
           "github_project_number": 1, "human_reviewers": ["operator"],
           "endpoints": [{"name": "custom", "kind": "openai"}]}
    with pytest.raises(ValueError, match=r"model_endpoints.*modes"):
        ProjectConfiguration(**raw)


def test_200_summary_uses_board_status_names(launch_config: ProjectConfiguration) -> None:
    text = running_summary(launch_config, board_columns={"demo": ["Ready", "In progress", "Done"]})
    assert "watches Ready" in text
    assert "watches To Do" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_200_container_probe_only_on_first_launch_or_doctor(
    launch_config: ProjectConfiguration, monkeypatch: pytest.MonkeyPatch, explicit: bool,
) -> None:
    config = ProjectConfiguration(preset="containers", harness="codex", github_org="example", github_repo="demo", github_project_number=1, human_reviewers=["operator"], state_file_path=launch_config.state_file_path)
    config.state_file_path.write_text("{}")
    monkeypatch.setattr(preflight, "check_directories", lambda config: [])
    monkeypatch.setattr(preflight, "check_port", lambda config: CheckResult("port", Status.OK, "free"))
    monkeypatch.setattr(preflight, "run_checks", lambda config: DoctorReport())
    monkeypatch.setattr(preflight, "check_github", AsyncMock(return_value=[]))
    monkeypatch.setattr(preflight, "check_native_models", lambda config: [])
    monkeypatch.setattr(preflight, "_local_harnesses", lambda config: [])
    containers = MagicMock(return_value=[])
    monkeypatch.setattr(preflight, "_container_harnesses", containers)
    await preflight.run_preflight(config, explicit=explicit)
    assert bool(containers.call_count) is explicit


@pytest.mark.parametrize("installed", [False, True])
def test_200_container_entrypoint_reuses_existing_codex(tmp_path: Path, installed: bool) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "npm-called"
    npm = bindir / "npm"
    npm.write_text('#!/bin/sh\nprintf called > "$NPM_LOG"\n')
    npm.chmod(0o755)
    codex = bindir / "codex"
    codex.write_text("#!/bin/sh\nexit " + ("0" if installed else "1") + "\n")
    codex.chmod(0o755)
    result = subprocess.run(
        ["/bin/sh", str(ROOT / "agent/performer/entrypoint.sh"), "/usr/bin/true"],
        env={**os.environ, "PATH": str(bindir), "BACKEND": "codex", "NPM_LOG": str(log),
             "PERFORMER_EGRESS_ALLOWLIST": "", "RTK_ENABLED": "0"},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert log.exists() is not installed


@pytest.mark.asyncio
async def test_200_runtime_bootstrap_defers_transient_board_initialization(
    launch_config: ProjectConfiguration, github_world: tuple, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, github = github_world
    github.initialize.side_effect = TransientGitHubError("offline")
    monkeypatch.setattr(cli, "GitHubService", MagicMock(return_value=github))
    monkeypatch.setattr(cli, "_build_transport", MagicMock(side_effect=RuntimeError("transport reached")))
    with pytest.raises(RuntimeError, match="transport reached"):
        await cli._bootstrap_services(launch_config, cli._build_circuit_breakers(launch_config))


@pytest.mark.asyncio
async def test_200_board_poll_retries_initialization_after_network_recovers(
    launch_config: ProjectConfiguration, monkeypatch: pytest.MonkeyPatch,
) -> None:
    github = preflight.GitHubService(
        launch_config.github_org, launch_config.github_project_number,
        auth=preflight.build_auth(launch_config),
    )
    recorder = MagicMock()
    monkeypatch.setattr("coordinare.services.github.SYSTEM_HEALTH", recorder)
    outcomes = iter([TimeoutError(), None])

    async def initialize_board() -> None:
        failure = next(outcomes)
        if failure is not None:
            raise failure
        github.project_id = "project"
        github.field_cache = {"status_option_ids": {"ready": "todo"}}

    initialize = AsyncMock(side_effect=initialize_board)
    monkeypatch.setattr(github, "initialize", initialize)
    monkeypatch.setattr(github, "_guarded_execute", AsyncMock(return_value={
        "node": {"items": {"nodes": [], "pageInfo": {"hasNextPage": False}}},
    }))
    with pytest.raises(TimeoutError):
        await github.poll_board()
    board = await github.poll_board()
    assert board["snapshot"]["TODO"] == []
    assert initialize.await_count > 1
    poll_outcomes = [call.kwargs["ok"] for call in recorder.record_board_poll.call_args_list]
    assert poll_outcomes == [False, True]
    await github.aclose()


def test_200_idle_explanation_is_scoped(launch_config: ProjectConfiguration) -> None:
    daemon = MagicMock()
    symphony = SymphonyRuntimeState(name="demo")
    symphony.board_snapshot = {"BLOCKED": ["card"]}
    daemon.state = {"config": launch_config, "phase": "idle", "symphony_configs": {"demo": MagicMock()},
                    "symphony_states": {"demo": symphony}}
    daemon.state_store.last_snapshot = None
    snapshot = DashboardStore().build_snapshot(daemon, MagicMock(), MagicMock())
    assert "idle_reason" not in snapshot
    assert "blocked" in snapshot["symphonies"][0]["idle_reason"]


@pytest.mark.asyncio
async def test_200_app_token_rate_limit_is_transient(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = tmp_path / "key.pem"
    key.write_text("test key")
    auth = AppAuth(app_id=1, installation_id=1, private_key_path=key)
    monkeypatch.setattr(auth, "_make_jwt", lambda: "test-jwt")
    client = AsyncMock()
    client.post.return_value = httpx.Response(httpx.codes.TOO_MANY_REQUESTS)
    client.__aenter__.return_value = client
    monkeypatch.setattr(httpx, "AsyncClient", MagicMock(return_value=client))
    with pytest.raises(TransientGitHubError):
        await auth.get_token()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [httpx.codes.FORBIDDEN, httpx.codes.NOT_FOUND])
async def test_200_graphql_rejection_is_deterministic(
    launch_config: ProjectConfiguration, code: int,
) -> None:
    github = preflight.GitHubService(
        launch_config.github_org, launch_config.github_project_number,
        auth=preflight.build_auth(launch_config),
    )
    client = MagicMock(execute_async=AsyncMock(side_effect=TransportServerError("rejected", code=code)))
    with pytest.raises(PermanentGitHubError):
        await github._execute_request(client, "{ __typename }", {})
    await github.aclose()


@pytest.mark.asyncio
async def test_200_harness_probe_timeout_warns_after_retry(
    launch_config: ProjectConfiguration, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preflight.shutil, "which", lambda name: "/bin/harness")
    probe = MagicMock(side_effect=subprocess.TimeoutExpired("harness", preflight.PROBE_TIMEOUT_SECONDS))
    monkeypatch.setattr(preflight.subprocess, "run", probe)
    results = await preflight.check_harnesses(launch_config)
    assert results and all(result.status is Status.WARN for result in results)
    assert probe.call_count > len(results)


@pytest.mark.asyncio
async def test_200_gql_connection_wrapper_remains_transient(launch_config: ProjectConfiguration) -> None:
    github = preflight.GitHubService(
        launch_config.github_org, launch_config.github_project_number,
        auth=preflight.build_auth(launch_config),
    )
    client = MagicMock(execute_async=AsyncMock(side_effect=TransportConnectionFailed("offline")))
    with pytest.raises(TransientGitHubError):
        await github._execute_request(client, "{ __typename }", {})
    await github.aclose()


@pytest.mark.asyncio
async def test_200_full_config_missing_blocked_reaches_runtime(
    launch_config: ProjectConfiguration, github_world: tuple, isolated_preflight: None,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    store = MagicMock(start=AsyncMock(), close=AsyncMock())
    runtime = AsyncMock()
    monkeypatch.setattr(cli, "EventStore", MagicMock(return_value=store))
    run_preflight_without_listeners(monkeypatch, runtime)
    await cli._run(launch_config.model_copy(update={"preset": None}))
    runtime.assert_awaited_once()
    output = capsys.readouterr().out
    assert "[WARN] board columns" in output
    assert "BLOCKED" in output


@pytest.mark.asyncio
async def test_200_doctor_includes_unused_model_catalogs(
    launch_config: ProjectConfiguration, isolated_preflight: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch_config.endpoints.append(launch_config.endpoints[0].model_copy(update={"name": "unused"}))
    monkeypatch.setattr(preflight, "check_github", AsyncMock(return_value=[]))
    native = MagicMock(return_value=[])
    monkeypatch.setattr(preflight, "check_native_models", native)
    await preflight.run_preflight(launch_config, explicit=True)
    assert {endpoint.name for endpoint in native.call_args.args[0].endpoints} == {"starter", "unused"}


@pytest.mark.asyncio
@pytest.mark.parametrize("check", ["check_directories", "check_harnesses", "check_native_models"])
async def test_200_new_checks_warn_for_existing_full_configs(
    launch_config: ProjectConfiguration, isolated_preflight: None, monkeypatch: pytest.MonkeyPatch,
    check: str,
) -> None:
    failure = [CheckResult("new setup check", Status.FAIL, "unavailable", "correct the setting")]
    probe = AsyncMock(return_value=failure) if check == "check_harnesses" else MagicMock(return_value=failure)
    monkeypatch.setattr(preflight, check, probe)
    monkeypatch.setattr(preflight, "check_github", AsyncMock(return_value=[]))
    config = launch_config.model_copy(update={"preset": None})
    report = await preflight.run_preflight(config)
    assert report.ok
    assert any(result.name == "new setup check" and result.status is Status.WARN for result in report.results)
