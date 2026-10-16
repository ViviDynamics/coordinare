from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import jsonschema
import pytest
import structlog
import yaml

from coordinare import __main__ as cli
from coordinare import launch_preflight
from coordinare.doctor import CheckResult, DoctorReport, Status
from coordinare.init_answers import ANSWER_NAMES

ROOT = Path(__file__).resolve().parents[2]
SECRET = "private-credential-201"
SECRET_MODE = 0o600


def invoke(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, dict]:
    monkeypatch.setattr("sys.argv", ["coordinare", *args])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    payload = json.loads(output.out)
    schema = json.loads((ROOT / "docs/schemas/setup-report.schema.json").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.Draft202012Validator(schema).validate(payload)
    assert payload["ok"] is (exited.value.code == 0)
    return exited.value.code, payload


@pytest.mark.parametrize("command", ["doctor", "validate"])
def test_invalid_json_command_syntax_uses_argparse_exit_two(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str) -> None:
    monkeypatch.setattr("sys.argv", ["coordinare", command, "--json", "--unknown-option"])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 2
    output = capsys.readouterr()
    assert not output.out
    assert "usage:" in output.err


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINARE_GITHUB_TOKEN", SECRET)
    monkeypatch.delenv("COORDINARE_CONFIG_PATH", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "preset": "solo", "harness": "codex", "github_org": "example",
        "github_repo": "demo", "github_project_number": 7,
        "human_reviewers": ["operator"], "github_token": "${COORDINARE_GITHUB_TOKEN}",
    }))
    return path


@pytest.mark.parametrize("command", [("validate",), ("config", "validate")])
def test_validate_json_success_and_missing_file(config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: tuple[str, ...]) -> None:
    code, report = invoke(monkeypatch, capsys, *command, "--json", "--config", str(config))
    assert code == 0
    assert report["command"] == "validate"
    assert report["checks"][0]["id"] == "config.schema"
    code, report = invoke(monkeypatch, capsys, *command, "--json", "--config", str(config / SECRET))
    assert code == 1
    assert report["checks"][0]["fix"]


@pytest.mark.parametrize("command", [("doctor",), ("config", "validate")])
@pytest.mark.parametrize("scenario", ["passing", "missing_file", "failing_check"])
def test_cli_reports_match_published_schema(config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: tuple[str, ...], scenario: str) -> None:
    # Exercise CLI parsing, config loading, real preflight and JSON serialization.
    # Only external GitHub, harness, provider and socket probes are replaced.
    monkeypatch.setattr(launch_preflight, "check_github", AsyncMock(return_value=[]))
    monkeypatch.setattr(launch_preflight, "check_harnesses", AsyncMock(return_value=[]))
    monkeypatch.setattr(launch_preflight, "check_native_models", Mock(return_value=[]))
    port_probe = Mock()
    monkeypatch.setattr(launch_preflight, "check_port_available", port_probe)
    raw = yaml.safe_load(config.read_text())
    raw["state_file_path"] = str(config.parent / "state" / "coordinare.state.json")
    if scenario == "failing_check":
        if command == ("doctor",):
            port_probe.side_effect = OSError("Port unavailable")
        else:
            raw["github_project_number"] = 0
    config.write_text(yaml.safe_dump(raw))
    path = config.parent / "missing.yaml" if scenario == "missing_file" else config

    # invoke validates the actual stdout document against setup-report.schema.json.
    code, report = invoke(monkeypatch, capsys, *command, "--config", str(path), "--json")
    assert code == (0 if scenario == "passing" else 1)
    assert report["command"] == ("doctor" if command == ("doctor",) else "validate")
    if scenario == "missing_file":
        finding = next(check for check in report["checks"] if check["field"] == "config_file")
        assert finding["id"] == "config.missing"
        assert "coordinare init" in finding["fix"]
        assert "--config <path>" in finding["fix"]
        port_probe.assert_not_called()
    elif command == ("doctor",):
        finding = next(check for check in report["checks"] if check["id"] == "doctor.dashboard_port")
        assert finding["status"] == ("ok" if scenario == "passing" else "fail")
        port_probe.assert_called_once()
    elif scenario == "failing_check":
        assert any(check["field"] == "github_project_number" and check["status"] == "fail" for check in report["checks"])


@pytest.mark.parametrize("command", ["validate", "doctor"])
@pytest.mark.parametrize("contents", ["github_token: [" + SECRET, "not: [valid", "- " + SECRET])
def test_invalid_yaml_is_json_without_input_or_traceback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str, contents: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(contents)
    code, report = invoke(monkeypatch, capsys, command, "--config", str(path), "--json")
    assert code == 1
    assert report["checks"][0]["status"] == "fail"


@pytest.mark.parametrize("strict,expected", [(False, 0), (True, 1)])
def test_validate_deprecation_and_placeholder_warnings(config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], strict: bool, expected: int) -> None:
    with config.open("a") as stream:
        stream.write("slack_channel: old\nenv_passthrough: [UNSET_201]\n")
    monkeypatch.delenv("UNSET_201", raising=False)
    flags = ["--strict"] if strict else []
    code, report = invoke(monkeypatch, capsys, "validate", "--config", str(config), "--json", *flags)
    assert code == expected
    assert any(check["id"] == "config.deprecated" for check in report["checks"])


@pytest.mark.parametrize("status,expected", [(Status.OK, 0), (Status.FAIL, 1), (Status.WARN, 0), (Status.SKIP, 0)])
def test_doctor_json_uses_real_verdict_but_never_echoes_probe_values(config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], status: Status, expected: int) -> None:
    probe = AsyncMock(return_value=DoctorReport([
        CheckResult("endpoint '" + SECRET + "'", status, "https://user:" + SECRET + "@host/?key=" + SECRET, "Fix " + SECRET),
        CheckResult("endpoint 'second'", Status.SKIP, "Native provider"),
    ]))
    monkeypatch.setattr(cli, "run_preflight", probe)
    code, report = invoke(monkeypatch, capsys, "doctor", "--config", str(config), "--json")
    assert code == expected
    assert report["checks"][0]["id"] == "doctor.endpoint"
    assert [check["instance"] for check in report["checks"] if check["id"] == "doctor.endpoint"] == [1, 2]
    assert report["checks"][0]["status"] == status.value
    probe.assert_awaited_once()
    assert probe.call_args.kwargs["explicit"] is True


def test_doctor_load_failure_is_json(config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def failed_load(*args: object) -> None:
        raise ValueError(SECRET)

    monkeypatch.setattr(cli, "_load_project_config_for_doctor", failed_load)
    code, report = invoke(monkeypatch, capsys, "doctor", "--config", str(config), "--json")
    assert code == 1
    assert report["checks"][0]["id"] == "config.load"


def test_doctor_unexpected_probe_failure_is_safe_json(config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "run_preflight", AsyncMock(side_effect=RuntimeError(SECRET)))
    code, report = invoke(monkeypatch, capsys, "doctor", "--config", str(config), "--json")
    assert code == 1
    assert any(check["id"] == "doctor.execution" for check in report["checks"])


@pytest.mark.parametrize("command", [("validate",), ("config", "validate"), ("doctor",)])
def test_json_accepts_config_before_subcommand(config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: tuple[str, ...]) -> None:
    monkeypatch.setattr(cli, "run_preflight", AsyncMock(return_value=DoctorReport()))
    code, _report = invoke(monkeypatch, capsys, "--config", str(config), *command, "--json")
    assert code == 0


def test_doctor_json_preserves_enabled_symphony_scope(config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    raw = yaml.safe_load(config.read_text())
    raw.pop("github_project_number")
    raw["symphonies"] = [
        {"name": "active", "github_project_number": 7},
        {"name": "disabled", "github_project_number": 8, "enabled": False},
    ]
    config.write_text(yaml.safe_dump(raw))
    probe = AsyncMock(return_value=DoctorReport())
    monkeypatch.setattr(cli, "run_preflight", probe)
    code, _report = invoke(monkeypatch, capsys, "doctor", "--config", str(config), "--json")
    assert code == 0
    root = probe.call_args.args[1]
    assert [(item.name, item.enabled) for item in root.symphonies] == [("active", True), ("disabled", False)]


def test_json_validates_environment_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("coordinare.config_discovery.Path.cwd", lambda: tmp_path)
    monkeypatch.setattr("coordinare.config_discovery.Path.home", lambda: tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("COORDINARE_CONFIG_PATH", raising=False)
    for key, value in {
        "PROJECT_NAME": "demo", "GITHUB_ORG": "example", "GITHUB_PROJECT_NUMBER": "7",
        "GITHUB_TOKEN": SECRET, "HUMAN_REVIEWERS": '["operator"]',
    }.items():
        monkeypatch.setenv("COORDINARE_" + key, value)
    code, _report = invoke(monkeypatch, capsys, "validate", "--json")
    assert code == 0


def test_answers_example_matches_published_schema_and_cli() -> None:
    schema = json.loads((ROOT / "docs/schemas/init-answers.schema.json").read_text())
    example = yaml.safe_load((ROOT / "docs/examples/init-answers.yaml").read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    validator = jsonschema.Draft202012Validator(schema)
    validator.validate(example)
    assert set(schema["properties"]) == set(ANSWER_NAMES)
    assert not {"github_token", "api_key"} & set(example)
    for invalid in ({"project_number": True}, {"harness": "unsupported"}, {"repository": "missing-owner"}, {"extra": "value"}):
        assert not validator.is_valid(invalid)


def test_setup_discovery_links_are_published() -> None:
    for filename in ("README.md", "AGENTS.md", "llms.txt"):
        assert "docs/setup-for-agents.md" in (ROOT / filename).read_text()


@pytest.mark.parametrize("command", ["validate", "doctor"])
def test_json_suppresses_config_and_probe_logs(config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str) -> None:
    with config.open("a") as stream:
        stream.write("cost_tracking:\n  cost_per_million_tokens: 1\n")

    async def noisy_probe(*args: object, **kwargs: object) -> DoctorReport:
        print(SECRET)
        print(SECRET, file=sys.stderr)
        structlog.get_logger("test201").warning("probe_failed", detail=SECRET)
        return DoctorReport([CheckResult("GitHub API", Status.WARN, "Retry", "Retry")])

    monkeypatch.setattr(cli, "run_preflight", noisy_probe)
    code, _report = invoke(monkeypatch, capsys, command, "--config", str(config), "--json")
    assert code == 0


@pytest.mark.parametrize("strict,expected", [(False, 0), (True, 1)])
def test_json_reports_placeholder_warning_without_prose(config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], strict: bool, expected: int) -> None:
    raw = yaml.safe_load(config.read_text())
    raw["performers"] = {"default": {"workflow_env": {"DOCS_ROOT": "${UNSET_201}"}}}
    config.write_text(yaml.safe_dump(raw))
    monkeypatch.delenv("UNSET_201", raising=False)
    flags = ["--strict"] if strict else []
    code, report = invoke(monkeypatch, capsys, "validate", "--config", str(config), "--json", *flags)
    assert code == expected
    finding = next(check for check in report["checks"] if check["id"] == "config.placeholder")
    assert finding["field"] == "performers"


def test_json_validation_never_echoes_invalid_values_or_unknown_keys(config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    raw = yaml.safe_load(config.read_text())
    raw.update({"github_project_number": SECRET, SECRET: SECRET})
    config.write_text(yaml.safe_dump(raw))
    code, report = invoke(monkeypatch, capsys, "validate", "--config", str(config), "--json")
    assert code == 1
    assert any(check["field"] == "github_project_number" for check in report["checks"])


def test_doctor_invalid_config_does_not_probe(config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    config.write_text("github_project_number: invalid\n")
    probe = AsyncMock()
    monkeypatch.setattr(cli, "run_preflight", probe)
    code, _report = invoke(monkeypatch, capsys, "doctor", "--config", str(config), "--json")
    assert code == 1
    probe.assert_not_awaited()


def test_doctor_json_opt_in_egress(config: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "run_preflight", AsyncMock(return_value=DoctorReport()))
    calls = []
    monkeypatch.setattr(cli, "check_kubernetes_egress_enforcement", lambda config: calls.append(config) or [CheckResult("egress enforcement", Status.SKIP, "Not Kubernetes")])
    code, report = invoke(monkeypatch, capsys, "doctor", "--config", str(config), "--json", "--check", "egress")
    assert code == 0
    assert len(calls) == 1
    assert report["checks"][0]["id"] == "doctor.egress"


def test_published_answers_initialize_with_private_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("COORDINARE_INIT_GITHUB_TOKEN", SECRET)
    monkeypatch.setenv("COORDINARE_INIT_API_KEY", SECRET)
    monkeypatch.setattr("coordinare.init_probes.read_command", AsyncMock(return_value=""))
    monkeypatch.setattr("coordinare.init_probes.check_token_and_repo", lambda *args, **kwargs: [])
    monkeypatch.setattr("coordinare.init_probes.check_github", AsyncMock(return_value=[]))
    monkeypatch.setattr("coordinare.init_probes.httpx.get", lambda *args, **kwargs: httpx.Response(200, json={"login": "operator"}))
    monkeypatch.setattr("coordinare.docker_setup.prepare_image", lambda *args: "ghcr.io/vividynamics/coordinare-performer-full:2026.10.84")
    monkeypatch.setattr("coordinare.docker_setup.smoke_container", lambda *args, **kwargs: None)
    monkeypatch.setattr("coordinare.init_cli.run_preflight", AsyncMock(return_value=DoctorReport()))
    output_dir = tmp_path / "setup"
    monkeypatch.setattr("sys.argv", ["coordinare", "init", "--non-interactive", "--answers", str(ROOT / "docs/examples/init-answers.yaml"), "--config-dir", str(output_dir)])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 0
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert SECRET not in (output_dir / "config.yaml").read_text()
    assert stat.S_IMODE((output_dir / "coordinare.env").stat().st_mode) == SECRET_MODE
    code, _report = invoke(monkeypatch, capsys, "validate", "--config", str(output_dir / "config.yaml"), "--json")
    assert code == 0
