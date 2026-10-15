from __future__ import annotations

import copy
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import yaml

import coordinare.graph.nodes.check_board as check_board_node
from coordinare import __main__ as cli
from coordinare.config import ProjectConfiguration
from coordinare.config_validation import validate_config
from coordinare.doctor import CheckResult, DoctorReport, Status
from coordinare.first_launch import (
    board_idle_explanation,
    idle_explanation,
    running_summary,
    sample_issue,
)
from coordinare.graph.nodes.dispatch_performer import dispatch_performer
from coordinare.graph.state import initial_state
from coordinare.launch_preflight import (
    check_directories,
    check_github,
    check_harnesses,
    check_native_models,
    check_port,
    run_preflight,
)
from coordinare.presets import expand_preset
from coordinare.protocol import ProtocolResponse
from coordinare.transport.subprocess_transport import SubprocessTransport
from coordinare.workspace import WorkspaceInfo

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def minimal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINARE_GITHUB_TOKEN", "test-token")
    return {
        "preset": "solo",
        "harness": "codex",
        "github_org": "example",
        "github_repo": "demo",
        "github_project_number": 1,
        "human_reviewers": ["operator"],
    }


def test_expansion_is_pure_and_explicit_settings_win(minimal: dict) -> None:
    minimal["performers"] = {"implementer": {"max_concurrency": 2}}
    minimal["max_concurrent_cards"] = 2
    before = copy.deepcopy(minimal)
    expanded = expand_preset(minimal)
    assert minimal == before
    assert expanded["performers"]["implementer"]["max_concurrency"] == 2
    config = ProjectConfiguration(**expanded)
    assert config.performers.resolved_role("implementer").backend == "codex"
    assert config.performers.reviewer is None
    assert config.resolve_performer_dispatch_model("implementer")["model"]


@pytest.mark.parametrize("preset", ["solo", "containers"])
@pytest.mark.parametrize("harness", ["codex", "claude_code", "opencode"])
def test_minimal_builds_real_dispatch_services(minimal: dict, preset: str, harness: str) -> None:
    config = ProjectConfiguration(**{**minimal, "preset": preset, "harness": harness})
    assert cli._build_lifecycle_sequence(config) == ["implementing"]
    if preset == "solo":
        services = cli._build_performer_services(config, cli._build_circuit_breakers(config))
        assert "implementing" in services
        transport = cli._build_transport(config)
        assert "performer" in transport._executable
        assert transport._build_subprocess_env()
    else:
        services, _ = cli._build_http_performer_services(config)
        assert len(services["implementing"]) == 1


def test_no_preset_changes_nothing() -> None:
    raw = {"github_org": "example", "human_reviewers": ["operator"], "github_token": "token"}
    assert expand_preset(raw) == raw
    config = ProjectConfiguration(**raw)
    assert config.performers.implementer is None
    assert config.agent_executable == ""
    assert config.endpoints == []


@pytest.mark.parametrize(
    "update,match",
    [
        ({"preset": "typo"}, "preset"),
        ({"harness": "typo"}, "harness"),
        ({"github_repo": ""}, "github_repo"),
        ({"harness": None}, "harness"),
        ({"harness": []}, "harness"),
    ],
)
def test_bad_starter_choices_name_the_setting(minimal: dict, update: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        ProjectConfiguration(**{**minimal, **update})


def test_minimal_example_validates_in_normal_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORDINARE_GITHUB_TOKEN", "test-token")
    path = ROOT / "config.minimal.yaml"
    assert len(path.read_text().splitlines()) <= 18
    result = validate_config(path)
    assert result.passed, result.errors


def test_config_command_shows_expansion_without_secrets(
    minimal: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(minimal))
    monkeypatch.setattr("sys.argv", ["coordinare", "--config", str(path), "config"])
    cli.main()
    output = capsys.readouterr().out
    assert "test-token" not in output
    assert "implementer" in output and "model_endpoints" in output


@pytest.mark.asyncio
async def test_start_checks_are_part_of_preflight(
    minimal: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ProjectConfiguration(**minimal)
    report = await run_preflight(config, raw={"github_token": "${MISSING_200_TOKEN}"})
    assert not report.ok
    assert "MISSING_200_TOKEN" in report.render()
    assert "Set" in report.render()


@pytest.mark.parametrize("kind", ["state_file_path", "performer_log_dir"])
def test_unwritable_directory_names_setting_and_fix(
    minimal: dict, tmp_path: Path, kind: str,
) -> None:
    obstacle = tmp_path / "file"
    obstacle.write_text("not a directory")
    value = obstacle / "state.json" if kind == "state_file_path" else obstacle / "logs"
    config = ProjectConfiguration(**{**minimal, kind: value})
    failures = [r for r in check_directories(config) if r.status is Status.FAIL]
    assert any(kind in r.name and kind in (r.fix or "") for r in failures)


def test_busy_dashboard_port_names_fix(minimal: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "coordinare.launch_preflight.check_port_available", MagicMock(side_effect=SystemExit(1)),
    )
    result = check_port(ProjectConfiguration(**minimal))
    assert result.status is Status.FAIL
    assert "8090" in result.detail
    assert "dashboard_port" in result.fix


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "broken", "timeout"])
async def test_harness_failure_is_actionable(
    minimal: dict, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    monkeypatch.setattr(
        "coordinare.launch_preflight.shutil.which",
        lambda name: None if failure == "missing" else "/bin/mock",
    )
    if failure == "timeout":
        probe = MagicMock(side_effect=subprocess.TimeoutExpired("codex", 5))
    else:
        probe = MagicMock(return_value=subprocess.CompletedProcess([], 1, "", "secret"))
    monkeypatch.setattr("coordinare.launch_preflight.subprocess.run", probe)
    results = await check_harnesses(ProjectConfiguration(**minimal))
    expected_status = Status.WARN if failure == "timeout" else Status.FAIL
    failures = [r for r in results if r.status is expected_status]
    assert failures and all(r.fix for r in failures)
    assert "codex" in " ".join(r.detail + (r.fix or "") for r in failures)
    assert "secret" not in str(results)


@pytest.fixture
def github_probes(monkeypatch: pytest.MonkeyPatch) -> tuple[MagicMock, MagicMock]:
    client = MagicMock()
    client.get.return_value = httpx.Response(
        200,
        headers={"X-OAuth-Scopes": "repo, project, read:org"},
        json={"permissions": {"push": True}},
    )
    monkeypatch.setattr("coordinare.launch_preflight.httpx.Client", MagicMock(return_value=client))
    client.__enter__.return_value = client
    github = MagicMock()
    github.initialize = AsyncMock()
    github.aclose = AsyncMock()
    github.field_cache = {
        "status_option_ids": {
            "to do": "1",
            "in progress": "2",
            "in review": "3",
            "blocked": "4",
            "done": "5",
        },
    }
    monkeypatch.setattr("coordinare.launch_preflight.GitHubService", MagicMock(return_value=github))
    return client, github


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure,fix",
    [
        ("token", "COORDINARE_GITHUB_TOKEN"),
        ("scopes", "project"),
        ("repo", "github_repo"),
        ("board", "github_project_number"),
        ("columns", "Status"),
    ],
)
async def test_github_failures_name_a_fix(
    minimal: dict, github_probes: tuple, failure: str, fix: str,
) -> None:
    client, github = github_probes
    if failure == "token":
        client.get.return_value = httpx.Response(401)
    elif failure == "scopes":
        client.get.return_value = httpx.Response(200, headers={"X-OAuth-Scopes": "read:org"})
    elif failure == "repo":
        client.get.side_effect = [
            httpx.Response(200, headers={"X-OAuth-Scopes": "repo, project, read:org"}),
            httpx.Response(404),
        ]
    elif failure == "board":
        github.initialize.side_effect = ValueError("private board secret")
    else:
        github.field_cache = {"status_option_ids": {"to do": "1"}}
    results = await check_github(ProjectConfiguration(**minimal))
    failures = [r for r in results if r.status is Status.FAIL]
    assert failures and any(fix in (r.fix or "") for r in failures)
    assert "private board secret" not in str(results)


@pytest.mark.asyncio
async def test_github_success_and_fine_grained_scope_header(
    minimal: dict, github_probes: tuple,
) -> None:
    client, github = github_probes
    client.get.return_value = httpx.Response(200, json={"permissions": {"push": True}})
    results = await check_github(ProjectConfiguration(**minimal))
    assert not [r for r in results if r.status is Status.FAIL]
    github.aclose.assert_awaited_once()
    assert any(r.status is Status.WARN and "permissions" in r.detail for r in results)


def test_running_summary_names_board_roles_skips_and_concurrency(minimal: dict) -> None:
    text = running_summary(ProjectConfiguration(**minimal))
    for expected in (
        "Coordinare is running",
        "example",
        "projects/1",
        "to do",
        "implementer",
        "reviewer",
        "not configured",
        "1",
        "http://127.0.0.1:8090",
    ):
        assert expected in text


@pytest.mark.parametrize(
    "counts,skips,expected",
    [
        ({"TODO": 0}, {}, "No cards"),
        ({"TODO": 2}, {"a": {"kind": "assignee_filter"}}, "assignee"),
        ({"TODO": 0, "BLOCKED": 2}, {}, "blocked"),
        ({"TODO": 2}, {"a": {"kind": "dependencies"}}, "waiting"),
    ],
)
def test_idle_explanation(counts: dict, skips: dict, expected: str) -> None:
    assert expected in idle_explanation(counts, skips)


def test_sample_issue_is_small_and_importable() -> None:
    issue = sample_issue()
    assert "FIRST_CARD.md" in issue["body"]
    assert "pull request" in issue["body"]
    assert issue["title"]


def test_preflight_report_has_failure_exit_contract() -> None:
    report = DoctorReport([CheckResult("test", Status.FAIL, "problem", "fix setting")])
    assert not report.ok
    assert "fix setting" in report.render()


@pytest.mark.parametrize("failure", ["missing", "unauthorized", "unreachable"])
def test_native_model_failure_names_credential(
    minimal: dict, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if failure != "missing":
        monkeypatch.setenv("OPENAI_API_KEY", "private-key")
    probe = MagicMock(return_value=httpx.Response(401))
    if failure == "unreachable":
        probe.side_effect = httpx.ConnectError("private-key")
    monkeypatch.setattr("coordinare.launch_preflight.httpx.get", probe)
    results = check_native_models(ProjectConfiguration(**minimal))
    assert results[0].status is (Status.WARN if failure == "unreachable" else Status.FAIL)
    assert "OPENAI_API_KEY" in results[0].fix
    assert "private-key" not in str(results)


def test_existing_native_cli_login_needs_no_new_api_key(
    minimal: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = ProjectConfiguration(**minimal)
    config.endpoints[0].auth_env = None
    assert not [r for r in check_native_models(config) if r.status is Status.FAIL]


@pytest.mark.asyncio
async def test_preflight_failure_prevents_runtime_bootstrap(
    minimal: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    report = DoctorReport([CheckResult("port", Status.FAIL, "busy port", "change dashboard_port")])
    preflight = AsyncMock(return_value=report)
    bootstrap = MagicMock()
    monkeypatch.setattr(cli, "run_preflight", preflight)
    monkeypatch.setattr(cli, "EventStore", bootstrap)
    with pytest.raises(SystemExit) as exc:
        await cli._run(ProjectConfiguration(**minimal))
    assert exc.value.code == 1
    bootstrap.assert_not_called()
    assert "change dashboard_port" in capsys.readouterr().out


def test_doctor_uses_same_preflight_and_exit_code(
    minimal: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(minimal))
    report = DoctorReport([CheckResult("port", Status.FAIL, "busy port", "change dashboard_port")])
    preflight = AsyncMock(return_value=report)
    monkeypatch.setattr(cli, "run_preflight", preflight)
    monkeypatch.setattr("sys.argv", ["coordinare", "doctor", "--config", str(path)])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    preflight.assert_awaited_once()
    assert preflight.call_args.kwargs["explicit"] is True
    assert "change dashboard_port" in capsys.readouterr().out


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["runtime", "image", "harness"])
async def test_container_failures_name_fix(
    minimal: dict, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    monkeypatch.setattr("coordinare.launch_preflight.shutil.which", lambda name: "/bin/mock")

    def probe(command: list, **kwargs: object) -> subprocess.CompletedProcess:
        fail = (
            (failure == "runtime" and command[1] == "info")
            or (failure == "image" and command[1] == "image")
            or (failure == "harness" and command[1] == "run")
        )
        return subprocess.CompletedProcess(command, int(fail), "", "private-key")

    monkeypatch.setattr("coordinare.launch_preflight.subprocess.run", probe)
    results = await check_harnesses(ProjectConfiguration(**{**minimal, "preset": "containers"}))
    failures = [r for r in results if r.status is Status.FAIL]
    assert len(failures) == 1
    assert failures[0].fix
    assert "private-key" not in str(results)


@pytest.mark.parametrize("setting", ["run_logs", "transcripts"])
def test_explicit_log_directory_failure_is_actionable(
    minimal: dict, tmp_path: Path, setting: str,
) -> None:
    obstacle = tmp_path / "file"
    obstacle.write_text("file")
    config = ProjectConfiguration(**{**minimal, setting: {"dir": str(obstacle / "logs")}})
    failures = [r for r in check_directories(config) if r.status is Status.FAIL]
    assert any(setting in r.name and setting in r.fix for r in failures)


def test_idle_waits_follow_actual_ownership_verdicts() -> None:
    message = board_idle_explanation({"TODO": ["a"]}, {"a": {"reason": "assigned_elsewhere"}}, {})
    assert "assignee_filter" in message
    assert board_idle_explanation({"TODO": ["a"]}, {}, {}) == ""
    assert board_idle_explanation(None, {}, {}) == ""


def test_idle_console_logs_once_per_reason_and_symphony(monkeypatch: pytest.MonkeyPatch) -> None:
    node = check_board_node
    logger = MagicMock()
    monkeypatch.setattr(node, "logger", logger)
    monkeypatch.setattr(node, "refresh_todo_waits", lambda *a, **k: None)
    monkeypatch.setattr(node, "_idle_messages", {})
    state = {"current_symphony": "alpha"}
    for _ in range(2):
        node._record_todo_waits(state, {"snapshot": {"TODO": []}}, [], [], None, {}, 1)
    logger.info.assert_called_once()
    state["current_symphony"] = "beta"
    node._record_todo_waits(state, {"snapshot": {"TODO": []}}, [], [], None, {}, 1)
    assert logger.info.call_count == 2


def test_client_idle_reason_scope_escape_and_first_card() -> None:
    result = subprocess.run(
        ["node", str(ROOT / "tests/js/first_launch_checks.js")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_config_output_redacts_environment_credentials(
    minimal: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    minimal["performers"] = {
        "implementer": {"workflow_env": {"OPENAI_API_KEY": "not-a-recognizable-secret"}},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(minimal))
    monkeypatch.setattr("sys.argv", ["coordinare", "config", "--config", str(path)])
    cli.main()
    assert "not-a-recognizable-secret" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_unused_catalog_entries_are_not_launch_dependencies(
    minimal: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ProjectConfiguration(**minimal)
    unused = config.endpoints[0].model_copy(
        update={"name": "unused", "auth_env": "MISSING_UNUSED_KEY"},
    )
    config.endpoints.append(unused)
    monkeypatch.setattr("coordinare.launch_preflight.check_directories", lambda c: [])
    monkeypatch.setattr(
        "coordinare.launch_preflight.check_port", lambda c: CheckResult("port", Status.OK, "free"),
    )
    monkeypatch.setattr("coordinare.launch_preflight.run_checks", lambda c: DoctorReport())
    monkeypatch.setattr("coordinare.launch_preflight.check_github", AsyncMock(return_value=[]))
    monkeypatch.setattr("coordinare.launch_preflight.check_harnesses", AsyncMock(return_value=[]))
    probe = MagicMock(return_value=[])
    monkeypatch.setattr("coordinare.launch_preflight.check_native_models", probe)
    await run_preflight(config)
    assert [e.name for e in probe.call_args.args[0].endpoints] == ["starter"]


@pytest.mark.asyncio
async def test_starter_card_dispatches_selected_harness_and_model(
    minimal: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ProjectConfiguration(**minimal)
    send = AsyncMock(
        side_effect=[
            ProtocolResponse(status="healthy"),
            ProtocolResponse(status="accepted", session_id="first-run"),
        ],
    )
    monkeypatch.setattr(SubprocessTransport, "send", send)
    services = cli._build_performer_services(config, cli._build_circuit_breakers(config))
    github = MagicMock()
    github.find_pr_for_issue = AsyncMock(return_value=None)
    github.count_closed_prs_for_issue = AsyncMock(return_value=0)
    github.list_prs_by_branch_prefix = AsyncMock(return_value=[])
    github.get_branch_head_sha = AsyncMock(return_value="main-sha")
    github.move_card = AsyncMock()
    workspace = MagicMock()
    workspace.prepare = AsyncMock(
        return_value=WorkspaceInfo(
            path=tmp_path,
            branch="coordinare/first/card",
            repo_url="https://github.com/example/demo.git",
            github_token="test-token",
        ),
    )
    state = initial_state()
    state.update(
        {
            "config": config,
            "github_service": github,
            "workspace_manager": workspace,
            "current_card": {"id": "first", "status": "TODO", "title": "First card"},
            "performer_services": services,
            "performer_stage": "implementing",
            "lifecycle_sequence": cli._build_lifecycle_sequence(config),
        },
    )
    result = await dispatch_performer(state)
    assert result["phase"] == "monitoring_performer"
    dispatch = send.await_args_list[-1].args[0]
    assert dispatch.payload["backend"] == "codex"
    assert dispatch.payload["model"] == "gpt-5.4"


def test_config_view_keeps_provider_env_names_visible(
    minimal: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(minimal))
    monkeypatch.setattr("sys.argv", ["coordinare", "config", "--config", str(path)])
    cli.main()
    view = yaml.safe_load(capsys.readouterr().out)
    assert view["github_auth"] == "pat"
    assert view["endpoints"][0]["auth_env"] == "OPENAI_API_KEY"


@pytest.mark.asyncio
async def test_mixed_performer_pools_probe_both_transports(
    minimal: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ProjectConfiguration(
        **{**minimal, "preset": "containers", "agent_executable": "/bin/local-performer"},
    )
    local = MagicMock(return_value=[])
    containers = MagicMock(return_value=[])
    monkeypatch.setattr("coordinare.launch_preflight._local_harnesses", local)
    monkeypatch.setattr("coordinare.launch_preflight._container_harnesses", containers)
    await check_harnesses(config)
    local.assert_called_once()
    containers.assert_called_once()


@pytest.mark.asyncio
async def test_compat_backend_uses_opencode_cli_name(
    minimal: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = ProjectConfiguration(**minimal)
    config.performers.implementer.backend = "opencode_compat"
    which = MagicMock(return_value="/bin/mock")
    monkeypatch.setattr("coordinare.launch_preflight.shutil.which", which)
    monkeypatch.setattr(
        "coordinare.launch_preflight.subprocess.run",
        MagicMock(return_value=subprocess.CompletedProcess([], 0, '{"status":"healthy"}', "")),
    )
    await check_harnesses(config)
    which.assert_called_with("opencode")


@pytest.mark.parametrize("args", [
    ["--config", "starter.yaml", "config", "validate"],
    ["config", "validate", "--config", "starter.yaml"],
    ["--config", "starter.yaml", "doctor"],
    ["doctor", "--config", "starter.yaml"],
])
def test_diagnostic_config_flag_is_preserved(args: list[str]) -> None:
    assert cli._build_arg_parser().parse_args(args).config == Path("starter.yaml")


@pytest.mark.asyncio
@pytest.mark.parametrize("network_error", [False, True])
async def test_persistent_performer_failure_names_endpoint_and_auth(
    minimal: dict, monkeypatch: pytest.MonkeyPatch, network_error: bool,
) -> None:
    config = ProjectConfiguration(**{
        **minimal, "preset": "containers",
        "performer_endpoints": [{"id": "remote", "mode": "persistent", "roles": ["implementer"], "image": "performer:test", "endpoint": "http://performer.example:8088"}],
    })
    probe = MagicMock(return_value=httpx.Response(401))
    if network_error:
        probe.side_effect = httpx.ConnectError("secret detail")
    monkeypatch.setattr("coordinare.launch_preflight.httpx.get", probe)
    results = await check_harnesses(config)
    assert results[0].status is (Status.WARN if network_error else Status.FAIL)
    assert "performer_endpoints[remote].endpoint" in results[0].fix
    assert "auth_token" in results[0].fix
    assert "secret detail" not in str(results)


@pytest.mark.asyncio
async def test_github_network_failure_names_api_setting(minimal: dict, github_probes: tuple) -> None:
    client, _ = github_probes
    client.get.side_effect = httpx.ConnectError("secret detail")
    results = await check_github(ProjectConfiguration(**minimal))
    assert results[0].status is Status.WARN
    assert "github_api_url" in results[0].fix
    assert "secret detail" not in str(results)


@pytest.mark.asyncio
async def test_app_token_failure_names_auth_settings(minimal: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("coordinare.launch_preflight.build_auth", MagicMock(side_effect=ValueError("secret detail")))
    results = await check_github(ProjectConfiguration(**minimal))
    assert results[0].status is Status.FAIL
    assert "github_private_key_path" in results[0].fix
    assert "secret detail" not in str(results)
