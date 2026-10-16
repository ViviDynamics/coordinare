from __future__ import annotations

import getpass
import os
import stat
import subprocess
import sys
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import yaml

from coordinare import __main__ as cli
from coordinare import init_files
from coordinare.config import ProjectConfiguration, expand_config_text
from coordinare.config_discovery import discover_config_path
from coordinare.config_validation import validate_config
from coordinare.dashboard.routers.config_write import _validate_global_config
from coordinare.doctor import CheckResult, DoctorReport, Status
from coordinare.init_answers import existing_defaults
from coordinare.init_files import minimal_template
from coordinare.init_probes import detected_defaults, list_projects, require_checks
from coordinare.service_manager import ServiceError
from coordinare.services.config_write_service import atomic_write_yaml
from coordinare.user_config import read_environment

SECRET_MODE = 0o600


@pytest.fixture
def setup_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    for name in ("COORDINARE_CONFIG_PATH", "COORDINARE_GITHUB_TOKEN", "GH_TOKEN",
                 "GITHUB_TOKEN", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name, raising=False)
    return tmp_path / "config" / "coordinare"


@pytest.fixture
def probes(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    monkeypatch.setattr("coordinare.init_probes.read_command", AsyncMock(return_value=""))
    monkeypatch.setattr("coordinare.init_probes.shutil.which", lambda name: "/bin/" + name)
    monkeypatch.setattr("coordinare.init_probes.check_token_and_repo", lambda *a, **k: [])
    monkeypatch.setattr("coordinare.init_probes.check_github", AsyncMock(return_value=[]))
    monkeypatch.setattr("coordinare.init_probes.check_harnesses", AsyncMock(return_value=[]))
    monkeypatch.setattr("coordinare.init_probes.httpx.get", MagicMock(return_value=httpx.Response(200, json={"login": "operator"})))
    monkeypatch.setattr("coordinare.init_probes.list_projects", lambda *a: [{"number": 7, "title": "Work"}])
    report = DoctorReport([CheckResult("setup", Status.OK, "Setup ready")])
    preflight = AsyncMock(return_value=report)
    monkeypatch.setattr("coordinare.init_cli.run_preflight", preflight)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    return preflight


def invoke(monkeypatch: pytest.MonkeyPatch, *args: str) -> int:
    monkeypatch.setattr("sys.argv", ["coordinare", "init", *args])
    with pytest.raises(SystemExit) as result:
        cli.main()
    return int(result.value.code)


def answers_file(tmp_path: Path, **overrides: object) -> Path:
    path = tmp_path / "answers.yaml"
    path.write_text(yaml.safe_dump({
        "github_token": "private-token", "repository": "example/demo",
        "project_number": 7, "harness": "codex", "api_key": "private-api-key",
        **overrides,
    }))
    return path


@pytest.mark.parametrize("harness", ["codex", "claude_code", "opencode"])
@pytest.mark.parametrize("run_mode", ["local", "docker"])
def test_answers_run_without_terminal_writes_valid_private_setup(
    setup_home: Path, probes: AsyncMock, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    harness: str, run_mode: str,
) -> None:
    answers = answers_file(tmp_path, harness=harness, run_mode=run_mode)
    assert invoke(monkeypatch, "--answers", str(answers)) == 0
    config_path = setup_home / "config.yaml"
    env_path = setup_home / "coordinare.env"
    text = config_path.read_text()
    assert "private-token" not in text and "private-api-key" not in text
    assert "${COORDINARE_GITHUB_TOKEN}" in text
    assert stat.S_IMODE(env_path.stat().st_mode) == SECRET_MODE
    assert validate_config(config_path).passed
    config = ProjectConfiguration.from_yaml(config_path)
    assert config.preset == ("solo" if run_mode == "local" else "containers")
    assert config.harness == harness
    assert config.human_reviewers == ["operator"]
    assert config.max_concurrent_cards == 1
    assert config.state_file_path == tmp_path / "data/coordinare/state.json"
    assert config.github_token.get_secret_value() == "private-token"
    assert config.resolve_performer_dispatch_model("implementer")
    probes.assert_awaited_once()
    assert probes.call_args.kwargs["explicit"] is True
    output = capsys.readouterr().out
    assert "Setup ready" in output and "http://127.0.0.1:8090" in output
    assert "private-token" not in output and "private-api-key" not in output
    assert "coordinare --config" in output


def test_interactive_defaults_with_stubbed_input(
    setup_home: Path, probes: AsyncMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setenv("GH_TOKEN", "private-token")
    monkeypatch.setenv("OPENAI_API_KEY", "private-api-key")
    responses = iter(["example/demo", "", "", "", "", "", "", "", "", ""])
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr("builtins.input", answer)
    # Credentials found in the environment are offered as saved values: Enter keeps them.
    monkeypatch.setattr("coordinare.init_answers.getpass.getpass", lambda prompt: "")
    assert invoke(monkeypatch) == 0
    assert prompts and all("[" in prompt and "]" in prompt for prompt in prompts)
    assert (setup_home / "config.yaml").is_file()
    assert (setup_home / "coordinare.env").read_text().count("private-token") == 1


def test_a_saved_credential_can_be_replaced_at_the_prompt(
    setup_home: Path, probes: AsyncMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setenv("GH_TOKEN", "stale-token")
    monkeypatch.setenv("OPENAI_API_KEY", "private-api-key")
    responses = iter(["example/demo", "", "", "", "", "", "", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda prompt: next(responses))
    typed = {"GitHub": "fresh-token"}
    monkeypatch.setattr(
        "coordinare.init_answers.getpass.getpass",
        lambda prompt: next((value for label, value in typed.items() if label.lower() in prompt.lower()), ""),
    )
    assert invoke(monkeypatch) == 0
    env = (setup_home / "coordinare.env").read_text()
    assert "fresh-token" in env
    assert "stale-token" not in env


def test_existing_files_require_force_and_are_preserved_with_backup(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = answers_file(tmp_path)
    assert invoke(monkeypatch, "--answers", str(answers)) == 0
    path = setup_home / "config.yaml"
    path.write_text(path.read_text() + "poll_interval_seconds: 45\n")
    original = path.read_bytes()
    env = (setup_home / "coordinare.env").read_bytes()
    assert invoke(monkeypatch, "--answers", str(answers)) == 1
    assert path.read_bytes() == original
    assert (setup_home / "coordinare.env").read_bytes() == env
    assert invoke(monkeypatch, "--answers", str(answers), "--force") == 0
    assert yaml.safe_load(path.read_text())["poll_interval_seconds"] == 45
    assert (setup_home / "config.yaml.bak").read_bytes() == original
    assert stat.S_IMODE((setup_home / "coordinare.env.bak").stat().st_mode) == SECRET_MODE


def test_missing_answers_are_reported_together_without_prompt(
    setup_home: Path, probes: AsyncMock, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("builtins.input", MagicMock(side_effect=AssertionError("prompted")))
    assert invoke(monkeypatch, "--non-interactive") == 1
    output = capsys.readouterr().err
    assert "github_token" in output and "repository" in output
    assert not setup_home.exists()
    probes.assert_not_awaited()


def test_preflight_failure_is_rendered_and_files_remain_for_repair(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    probes.return_value = DoctorReport([CheckResult("port", Status.FAIL, "busy", "Set dashboard_port")])
    assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path))) == 1
    assert (setup_home / "config.yaml").exists()
    assert "Set dashboard_port" in capsys.readouterr().out


def test_xdg_fallback_preserves_checkout_and_explicit_precedence(
    setup_home: Path, tmp_path: Path,
) -> None:
    setup_home.mkdir(parents=True)
    user_config = setup_home / "config.yaml"
    user_config.write_text("project_name: user\n")
    assert discover_config_path(None) == user_config
    checkout = tmp_path / "config.yaml"
    checkout.write_text("project_name: checkout\n")
    assert discover_config_path(None) == checkout
    assert discover_config_path(user_config) == user_config


@pytest.mark.parametrize("kind,fix", [("token", "valid token"), ("scopes", "project"), ("repository", "github_repo"), ("columns", "IN_REVIEW"), ("harness", "Install codex")])
def test_failed_question_names_fix_before_writing(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], kind: str, fix: str,
) -> None:
    result = CheckResult(kind, Status.FAIL, "Setup question failed", fix)
    if kind in {"token", "scopes", "repository"}:
        monkeypatch.setattr("coordinare.init_probes.check_token_and_repo", lambda *a, **k: [result])
    else:
        name = "check_github" if kind == "columns" else "check_harnesses"
        monkeypatch.setattr("coordinare.init_probes." + name, AsyncMock(return_value=[result]))
    assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path))) == 1
    assert fix in capsys.readouterr().err
    assert not setup_home.exists()
    probes.assert_not_awaited()


@pytest.mark.parametrize("secret", ["secret 'quoted' \"double\" $HOME $(touch nope) `whoami` # comment", "secret: colon", "[secret,sequence]"])
def test_credentials_round_trip_literally_without_yaml_or_shell_execution(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], secret: str,
) -> None:
    answers = answers_file(tmp_path, github_token=secret, api_key=secret)
    assert invoke(monkeypatch, "--answers", str(answers)) == 0
    values = read_environment(setup_home / "coordinare.env")
    assert values["COORDINARE_GITHUB_TOKEN"] == secret
    assert values["OPENAI_API_KEY"] == secret
    config = ProjectConfiguration.from_yaml(setup_home / "config.yaml")
    assert config.github_token.get_secret_value() == secret
    assert secret not in (setup_home / "config.yaml").read_text()
    output = capsys.readouterr()
    assert secret not in output.out + output.err
    assert not (tmp_path / "nope").exists()


def test_flags_override_environment_and_answers_and_support_custom_model_endpoint(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORDINARE_INIT_MAX_CONCURRENT_CARDS", "2")
    answers = answers_file(tmp_path, max_concurrent_cards=3)
    assert invoke(monkeypatch, "--answers", str(answers), "--max-concurrent-cards", "4",
                  "--model", "local/model", "--endpoint", "http://models.example/v1",
                  "--human-reviewers", "alice,bob") == 0
    config = ProjectConfiguration.from_yaml(setup_home / "config.yaml")
    assert config.max_concurrent_cards == 4
    assert config.human_reviewers == ["alice", "bob"]
    assert config.endpoints[0].kind == "litellm"
    assert str(config.endpoints[0].base_url).rstrip("/") == "http://models.example/v1"
    assert config.resolve_performer_dispatch_model("implementer")["model"] == "local/model"


def test_environment_only_setup_without_terminal(
    setup_home: Path, probes: AsyncMock, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in {"GITHUB_TOKEN": "private-token", "REPOSITORY": "example/demo",
                       "PROJECT_NUMBER": "7", "HARNESS": "opencode", "API_KEY": "private-api-key"}.items():
        monkeypatch.setenv("COORDINARE_INIT_" + key, value)
    assert invoke(monkeypatch, "--non-interactive") == 0
    assert (setup_home / "config.yaml").exists()


def test_force_reuses_saved_answers_and_preserves_model_endpoint(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = answers_file(tmp_path, model="custom-model", endpoint="http://models.example/v1")
    assert invoke(monkeypatch, "--answers", str(answers)) == 0
    assert invoke(monkeypatch, "--force", "--non-interactive") == 0
    config = ProjectConfiguration.from_yaml(setup_home / "config.yaml")
    assert config.resolve_performer_dispatch_model("implementer")["model"] == "custom-model"
    assert str(config.endpoints[0].base_url).rstrip("/") == "http://models.example/v1"


@pytest.mark.parametrize("contents", ["[]", "github_token: [private-token]", "repository: {}", "max_concurrent_cards: true", "harness: unsupported", "42: ignored", "42: ignored\nunknown: answer"])
def test_invalid_answers_fail_without_echoing_values_or_writing(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], contents: str,
) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(contents)
    assert invoke(monkeypatch, "--answers", str(path)) == 1
    output = capsys.readouterr()
    assert "private-token" not in output.out + output.err
    assert not setup_home.exists()


def test_fine_grained_permissions_warning_is_shown_without_rejecting_valid_read_access(
    capsys: pytest.CaptureFixture[str],
) -> None:
    require_checks([CheckResult("GitHub permissions", Status.WARN, "Write access cannot be proven", "Grant Projects write access")])
    assert "Write access cannot be proven" in capsys.readouterr().out


def test_detection_reuses_gh_login_git_remote_and_installed_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("COORDINARE_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    command = AsyncMock(side_effect=["private-token", "git@github.com:example/demo.git"])
    monkeypatch.setattr("coordinare.init_probes.read_command", command)
    monkeypatch.setattr("coordinare.init_probes.shutil.which", lambda name: "/bin/claude" if name == "claude" else None)
    defaults = detected_defaults()
    assert defaults == {"github_token": "private-token", "repository": "example/demo", "harness": "claude_code"}
    assert command.call_args_list[0].args == ("gh", "auth", "token")


def test_project_listing_paginates_users_and_organizations(monkeypatch: pytest.MonkeyPatch) -> None:
    post = MagicMock(side_effect=[
        httpx.Response(200, json={"data": {"repositoryOwner": {"projectsV2": {"nodes": [{"number": 1, "title": "Closed", "closed": True}], "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}}}),
        httpx.Response(200, json={"data": {"repositoryOwner": {"projectsV2": {"nodes": [{"number": 7, "title": "Work", "closed": False}], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}),
    ])
    monkeypatch.setattr("coordinare.init_probes.httpx.post", post)
    assert list_projects("example", "private-token") == [{"number": 7, "title": "Work", "closed": False}]
    assert post.call_args.kwargs["json"]["variables"]["after"] == "next"
    query = post.call_args.kwargs["json"]["query"]
    assert "on User" in query and "on Organization" in query


def test_generated_env_loads_in_fresh_process_and_exported_values_win(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path))) == 0
    child_environment = os.environ.copy()
    child_environment.pop("COORDINARE_GITHUB_TOKEN", None)
    child_environment.pop("OPENAI_API_KEY", None)
    script = (
        "from coordinare.config import ProjectConfiguration; import sys; "
        "config = ProjectConfiguration.from_yaml(sys.argv[1]); "
        "sys.exit(0 if config.github_token.get_secret_value() == 'private-token' else 1)"
    )
    child = subprocess.run([sys.executable, "-c", script, str(setup_home / "config.yaml")],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           env=child_environment, check=False)
    assert child.returncode == 0, child.stderr
    monkeypatch.delenv("COORDINARE_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert validate_config(setup_home / "config.yaml").passed
    config = ProjectConfiguration.from_yaml(setup_home / "config.yaml")
    assert config.github_token.get_secret_value() == "private-token"
    monkeypatch.setenv("COORDINARE_GITHUB_TOKEN", "exported-token")
    assert ProjectConfiguration.from_yaml(setup_home / "config.yaml").github_token.get_secret_value() == "exported-token"


@pytest.mark.parametrize("force", [False, True])
def test_failed_config_write_rolls_back_env_file(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    force: bool,
) -> None:
    original = init_files._write_atomic
    if force:
        assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path))) == 0
    saved_env = (setup_home / "coordinare.env").read_bytes() if force else None

    def fail_config(path: Path, content: str, *, replace: bool) -> None:
        if path.name == "config.yaml":
            raise OSError("simulated disk failure")
        original(path, content, replace=replace)

    monkeypatch.setattr(init_files, "_write_atomic", fail_config)
    args = ["--force"] if force else []
    assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path, github_token="replacement-token")), *args) == 1
    if force:
        assert (setup_home / "coordinare.env").read_bytes() == saved_env
    else:
        assert not (setup_home / "coordinare.env").exists()


def test_packaged_minimal_template_uses_resources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    resource = tmp_path / "data"
    resource.mkdir()
    (resource / "config.minimal.yaml").write_text("preset: solo\nharness: opencode\n")
    lookup = MagicMock(return_value=tmp_path)
    monkeypatch.setattr("coordinare.init_files.files", lookup)
    assert minimal_template() == {"preset": "solo", "harness": "opencode"}
    lookup.assert_called_once_with("coordinare")


def test_secret_prompt_fails_closed_if_terminal_cannot_hide_input(
    setup_home: Path, probes: AsyncMock, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    def insecure_prompt(prompt: str) -> str:
        warnings.warn("Cannot hide input", getpass.GetPassWarning, stacklevel=2)
        return "private-token"

    monkeypatch.setattr("coordinare.init_answers.getpass.getpass", insecure_prompt)
    assert invoke(monkeypatch) == 1
    assert "terminal" in capsys.readouterr().err
    assert not setup_home.exists()


@pytest.mark.parametrize("replacement", [
    {"endpoint": "", "model": "gpt-5.4"},
    {"harness": "claude_code", "endpoint": "", "model": "claude-sonnet-4-6"},
])
def test_force_replaces_starter_choices_and_preserves_unrelated_catalog_entries(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    replacement: dict[str, str],
) -> None:
    answers = answers_file(tmp_path, model="old-model", endpoint="http://old.example/v1")
    assert invoke(monkeypatch, "--answers", str(answers)) == 0
    config_path = setup_home / "config.yaml"
    raw = yaml.safe_load(config_path.read_text())
    extra = {"name": "unrelated", "kind": "ollama", "base_url": "http://other.example"}
    raw["endpoints"].append(extra)
    config_path.write_text(yaml.safe_dump(raw))
    argv = [arg for key, value in replacement.items() for arg in ("--" + key.replace("_", "-"), value)]
    assert invoke(monkeypatch, "--answers", str(answers), "--force", *argv) == 0
    config = ProjectConfiguration.from_yaml(config_path)
    starter = next(endpoint for endpoint in config.endpoints if endpoint.name == "starter")
    assert starter.kind == ("anthropic" if replacement.get("harness") == "claude_code" else "openai")
    assert starter.base_url is None
    assert config.resolve_performer_dispatch_model("implementer")["model"] == replacement["model"]
    assert extra in yaml.safe_load(config_path.read_text())["endpoints"]


def test_rerun_can_enable_api_key_after_harness_login(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path, api_key=""))) == 0
    assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path)), "--force") == 0
    config = ProjectConfiguration.from_yaml(setup_home / "config.yaml")
    assert config.endpoints[0].auth_env == "OPENAI_API_KEY"


def test_dashboard_save_preserves_safe_credential_expansion_without_comments(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path, github_token="secret: colon"))) == 0
    path = setup_home / "config.yaml"
    raw = yaml.safe_load(path.read_text())
    validate_config(path)
    updated = {**raw, "poll_interval_seconds": 60}
    _validate_global_config(updated, original=raw)
    atomic_write_yaml(path, updated)
    assert validate_config(path).passed
    assert ProjectConfiguration.from_yaml(path).github_token.get_secret_value() == "secret: colon"
    assert "secret: colon" not in path.read_text()


def test_saved_token_beats_detected_gh_login(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("COORDINARE_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("coordinare.init_probes.read_command", AsyncMock(side_effect=["detected-login", ""]))
    values = existing_defaults({}, {"COORDINARE_GITHUB_TOKEN": "saved-token"})
    assert values["github_token"] == "saved-token"


def test_invalid_secret_env_returns_sanitized_validation_error(
    setup_home: Path,
) -> None:
    setup_home.mkdir(parents=True)
    path = setup_home / "config.yaml"
    path.write_text("preset: solo\nharness: codex\ngithub_org: example\ngithub_repo: demo\ngithub_project_number: 7\nhuman_reviewers: [operator]\n")
    (setup_home / "coordinare.env").write_text("COORDINARE_GITHUB_TOKEN='private-token\n")
    result = validate_config(path)
    assert not result.passed
    assert "private-token" not in str(result.errors)
    assert "coordinare.env" in str(result.errors)


def test_force_preserves_starter_settings_outside_setup_answers(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = answers_file(tmp_path, model="custom-model", endpoint="http://models.example/v1")
    assert invoke(monkeypatch, "--answers", str(answers)) == 0
    path = setup_home / "config.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["model_endpoints"][0]["reasoning_policy"] = "disable_thinking"
    raw["endpoints"][0]["kind"] = "vllm"
    path.write_text(yaml.safe_dump(raw))
    assert invoke(monkeypatch, "--answers", str(answers), "--force") == 0
    saved = yaml.safe_load(path.read_text())
    assert saved["model_endpoints"][0]["reasoning_policy"] == "disable_thinking"
    assert saved["endpoints"][0]["kind"] == "vllm"


def test_start_flag_hands_generated_config_to_daemon_without_binding_sockets(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = AsyncMock()
    monkeypatch.setattr(cli, "_run", run)
    monkeypatch.setattr("sys.argv", ["coordinare", "init", "--answers", str(answers_file(tmp_path)), "--start"])
    cli.main()
    run.assert_awaited_once()
    assert run.call_args.args[0].github_repo == "demo"
    assert run.call_args.kwargs["config_path"] == setup_home / "config.yaml"


def test_explicit_token_is_used_for_board_question_probes(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORDINARE_GITHUB_TOKEN", "stale-exported-token")
    board = AsyncMock(return_value=[])
    monkeypatch.setattr("coordinare.init_probes.check_github", board)
    assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path))) == 0
    assert board.call_args.args[0].github_token.get_secret_value() == "private-token"


def test_unresponsive_harness_stops_before_later_questions(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    prompts: list[str] = []

    def accept_default(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    monkeypatch.setattr("builtins.input", accept_default)
    result = CheckResult("harness", Status.FAIL, "codex did not answer", "Install and authenticate codex")
    monkeypatch.setattr("coordinare.init_probes.check_harnesses", AsyncMock(return_value=[result]))
    assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path))) == 1
    assert not any("Concurrent cards" in prompt for prompt in prompts)
    assert not any("Human reviewer" in prompt for prompt in prompts)


@pytest.mark.parametrize("confirmation", ["", "no", "yes"])
@pytest.mark.parametrize("force", [False, True])
def test_interactive_rerun_offers_saved_defaults_and_requires_confirmation(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], confirmation: str, force: bool,
) -> None:
    answers = answers_file(
        tmp_path, repository="saved/repository", project_number=23, harness="claude_code",
        model="saved-model", endpoint="http://saved.example/v1", max_concurrent_cards=3,
        human_reviewers=["alice", "bob"], run_mode="docker",
    )
    assert invoke(monkeypatch, "--answers", str(answers)) == 0
    config_path = setup_home / "config.yaml"
    env_path = setup_home / "coordinare.env"
    config_path.write_text(config_path.read_text() + "poll_interval_seconds: 45\n")
    original_config, original_env = config_path.read_bytes(), env_path.read_bytes()
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    prompts: list[str] = []

    def accept_default(prompt: str) -> str:
        prompts.append(prompt)
        return confirmation if "replace" in prompt.lower() else ""

    monkeypatch.setattr("builtins.input", accept_default)
    secret_prompts: list[str] = []

    def keep_saved(prompt: str) -> str:
        # Enter keeps a saved credential; the prompt never shows it.
        secret_prompts.append(prompt)
        return ""

    monkeypatch.setattr("coordinare.init_answers.getpass.getpass", keep_saved)
    probes.reset_mock()
    result = invoke(monkeypatch, *(["--force"] if force else []))
    assert result == (0 if confirmation == "yes" else 1)
    for default in ("saved/repository", "23", "claude_code", "saved-model",
                    "http://saved.example/v1", "3", "alice,bob", "docker"):
        assert any(f"[{default}]" in prompt for prompt in prompts), default
    assert any("replace" in prompt.lower() and "[no]" in prompt for prompt in prompts)
    if confirmation == "yes":
        saved = yaml.safe_load(config_path.read_text())
        assert saved["poll_interval_seconds"] == 45
        assert saved["max_concurrent_cards"] == 3
        assert env_path.read_bytes() == original_env
        assert (setup_home / "config.yaml.bak").read_bytes() == original_config
        assert (setup_home / "coordinare.env.bak").read_bytes() == original_env
        assert stat.S_IMODE((setup_home / "coordinare.env.bak").stat().st_mode) == SECRET_MODE
        probes.assert_awaited_once()
    else:
        assert config_path.read_bytes() == original_config
        assert env_path.read_bytes() == original_env
        assert not (setup_home / "config.yaml.bak").exists()
        assert not (setup_home / "coordinare.env.bak").exists()
        probes.assert_not_awaited()
    output = capsys.readouterr()
    shown = output.out + output.err + "".join(prompts) + "".join(secret_prompts)
    assert "private-token" not in shown
    assert "private-api-key" not in shown
    assert all("saved credential, Enter keeps it" in prompt for prompt in secret_prompts)


@pytest.mark.parametrize("flag,variable", [
    ("--github-token", "COORDINARE_INIT_GITHUB_TOKEN"),
    ("--api-key", "COORDINARE_INIT_API_KEY"),
])
def test_secret_cli_flag_warns_without_echoing_value(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], flag: str, variable: str,
) -> None:
    assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path)), flag, "secret-cli-value") == 0
    output = capsys.readouterr()
    assert "warning" in output.err.lower()
    assert flag in output.err and variable in output.err and "--answers" in output.err
    assert "shell history" in output.err and "process" in output.err
    assert "secret-cli-value" not in output.out + output.err


def test_secrets_from_answers_do_not_warn_about_command_line(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path))) == 0
    assert "shell history" not in capsys.readouterr().err


@pytest.mark.parametrize("contents", ["not-an-assignment", "COORDINARE_GITHUB_TOKEN='private-token"])
def test_from_yaml_wraps_malformed_secret_env_as_config_read_error(
    setup_home: Path, contents: str,
) -> None:
    setup_home.mkdir(parents=True)
    path = setup_home / "config.yaml"
    path.write_text("preset: solo\nharness: codex\n")
    (setup_home / "coordinare.env").write_text(contents)
    with pytest.raises(OSError, match=r"Failed to read config file.*coordinare\.env") as exc:
        ProjectConfiguration.from_yaml(path)
    assert "private-token" not in str(exc.value)


def test_init_uses_public_github_token_and_repo_probe() -> None:
    from coordinare import init_probes, launch_preflight

    assert init_probes.check_token_and_repo is launch_preflight.check_token_and_repo


def test_generated_secrets_filename_is_gitignored() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"], input="coordinare.env\n",
        cwd=root, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "coordinare.env"


@pytest.mark.parametrize("answer", ["yes", "no"])
def test_init_offers_login_service_without_duplicate_foreground_start(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    answer: str,
) -> None:
    install = MagicMock()
    monkeypatch.setattr("coordinare.init_cli.install_service", install)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    prompts: list[str] = []

    def respond(prompt: str) -> str:
        prompts.append(prompt)
        return answer if "start at login" in prompt.lower() else ""

    monkeypatch.setattr("builtins.input", respond)
    assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path))) == 0
    assert any("start at login" in prompt.lower() for prompt in prompts)
    if answer == "yes":
        install.assert_called_once_with(setup_home / "config.yaml")
        assert not any("Start Coordinare now" in prompt for prompt in prompts)
    else:
        install.assert_not_called()


def test_noninteractive_login_flag_calls_shared_installer(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install = MagicMock()
    monkeypatch.setattr("coordinare.init_cli.install_service", install)
    assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path)), "--start-at-login") == 0
    install.assert_called_once_with(setup_home / "config.yaml")


def test_service_install_failure_keeps_successful_setup(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install = MagicMock(side_effect=ServiceError("User session unavailable"))
    monkeypatch.setattr("coordinare.init_cli.install_service", install)
    assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path)), "--start-at-login") == 1
    assert (setup_home / "config.yaml").exists()
    assert (setup_home / "coordinare.env").exists()


def test_failed_preflight_never_installs_service(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    install = MagicMock()
    probes.return_value = DoctorReport([CheckResult("setup", Status.FAIL, "Not ready", "Repair setup")])
    monkeypatch.setattr("coordinare.init_cli.install_service", install)
    assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path)), "--start-at-login") == 1
    install.assert_not_called()


@pytest.mark.parametrize("value", ["literal: colon", "[one, two]", "first\nsecond", "${UNSET_195_VALUE}"])
def test_preset_env_expansion_matches_validation_and_daemon_loading(
    setup_home: Path, probes: AsyncMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    assert invoke(monkeypatch, "--answers", str(answers_file(tmp_path))) == 0
    path = setup_home / "config.yaml"
    path.write_text(path.read_text() + 'project_name: "${PRESET_195_VALUE}"\n')
    monkeypatch.setenv("PRESET_195_VALUE", value)
    monkeypatch.delenv("UNSET_195_VALUE", raising=False)
    expanded = yaml.safe_load(expand_config_text(path.read_text()))
    assert expanded["project_name"] == value
    assert isinstance(expanded["project_name"], str)
    assert validate_config(path).passed
    assert ProjectConfiguration.from_yaml(path).project_name == value
