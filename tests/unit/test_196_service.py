from __future__ import annotations

import argparse
import asyncio
import json
import os
import plistlib
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from coordinare import __main__ as cli
from coordinare import service_cli, service_dev
from coordinare.service_files import (
    LABEL,
    ServiceDefinition,
    render_plist,
    render_unit,
    render_wrapper,
)
from coordinare.service_manager import (
    COMMAND_TIMEOUT_SECONDS,
    ServiceError,
    ServiceManager,
    dashboard_url,
    run_command,
)

SECRET = "private-service-token"


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.output = "ActiveState=active\nExecMainStartTimestamp=Thu 2026-10-15 10:00:00 UTC\n"
        self.returncode = 0

    def __call__(self, argv: list[str], *, capture: bool = True) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.returncode, self.output, "")


@pytest.fixture
def definition(tmp_path: Path) -> ServiceDefinition:
    directory = tmp_path / "user files % and $ cash's"
    directory.mkdir()
    config = directory / "config.yaml"
    config.write_text("dashboard_host: 0.0.0.0\ndashboard_port: 8123\n")
    env = directory / "coordinare.env"
    env.write_text(f"COORDINARE_GITHUB_TOKEN={shlex.quote(SECRET)}\n")
    return ServiceDefinition(
        config=config, env=env, command=(str(directory / "bin" / "coordinare"),),
        working_directory=directory, version="2026.10.99",
    )


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data home"))
    return tmp_path


def test_plist_has_login_failure_restart_and_literal_paths(definition: ServiceDefinition, tmp_path: Path) -> None:
    wrapper, log = tmp_path / "run service", tmp_path / "daemon log"
    content = render_plist(definition, wrapper, log)
    parsed = plistlib.loads(content)
    assert parsed["Label"] == LABEL
    assert parsed["ProgramArguments"] == ["/bin/sh", str(wrapper)]
    assert parsed["WorkingDirectory"] == str(definition.working_directory)
    assert parsed["RunAtLoad"] is True
    assert parsed["KeepAlive"] == {"SuccessfulExit": False}
    assert parsed["StandardOutPath"] == str(log)
    assert parsed["StandardErrorPath"] == str(log)
    assert SECRET not in content.decode()


def test_wrapper_references_env_and_quotes_each_argument(definition: ServiceDefinition) -> None:
    content = render_wrapper(definition)
    lines = content.splitlines()
    assert f". {shlex.quote(str(definition.env))}" in lines
    assert "set -a" in lines and "set +a" in lines
    assert shlex.split(lines[-1]) == ["exec", *definition.command, "--config", str(definition.config)]
    assert SECRET not in content


def test_systemd_unit_escapes_spaces_specifiers_and_dollars(definition: ServiceDefinition) -> None:
    content = render_unit(definition)
    assert "WantedBy=default.target" in content
    assert "Restart=on-failure" in content
    assert "EnvironmentFile=" + str(definition.env).replace("%", "%%") + "\n" in content
    assert "WorkingDirectory=" + str(definition.working_directory).replace("%", "%%") + "\n" in content
    assert str(definition.env).replace("%", "%%") in content
    assert str(definition.config).replace("%", "%%").replace("$", "$$") in content
    assert "ExecStart=\"" in content
    assert SECRET not in content


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_install_start_stop_restart_logs_uninstall_preserve_data(
    home: Path, definition: ServiceDefinition, system: str,
) -> None:
    runner = FakeRunner()
    manager = ServiceManager(system=system, runner=runner)
    manager.install(definition)
    state = manager.directory.parent / "state.json"
    state.write_text("preserved daemon state")
    manager.log_path.write_text("preserved daemon log")
    assert manager.unit_path.exists()
    assert SECRET not in manager.unit_path.read_text()
    assert SECRET not in manager.metadata_path.read_text()
    if system == "Linux":
        assert manager.unit_path == home / "config home/systemd/user/coordinare.service"
        assert ["systemctl", "--user", "enable", "coordinare.service"] in runner.calls
    else:
        assert manager.unit_path == home / f"Library/LaunchAgents/{LABEL}.plist"
        assert any("bootstrap" in call for call in runner.calls)
    manager.stop()
    manager.start()
    manager.restart()
    manager.logs()
    assert any("journalctl" in call or "tail" in call for call in runner.calls)
    runner.calls.clear()
    manager.uninstall()
    assert not manager.unit_path.exists()
    assert not manager.metadata_path.exists()
    assert definition.config.exists() and definition.env.exists()
    assert manager.log_path.parent.exists()
    assert state.read_text() == "preserved daemon state"
    assert manager.log_path.read_text() == "preserved daemon log"
    assert runner.calls


def test_status_uses_registered_config_and_includes_start_time(home: Path, definition: ServiceDefinition) -> None:
    runner = FakeRunner()
    ServiceManager(system="Linux", runner=runner).install(definition)
    manager = ServiceManager(system="Linux", runner=runner)
    status = manager.status()
    for text in ("running", "2026-10-15", "2026.10.99", str(definition.config), "http://127.0.0.1:8123"):
        assert text in status
    assert SECRET not in status
    runner.output = "ActiveState=inactive\nExecMainStartTimestamp=\n"
    assert "not running" in manager.status()


def test_mac_status_reads_pid_start_time(home: Path, definition: ServiceDefinition) -> None:
    runner = FakeRunner()
    manager = ServiceManager(system="Darwin", runner=runner)
    manager.install(definition)
    runner.output = "state = running\n pid = 12345\n"
    status = manager.status()
    assert "running" in status and str(definition.config) in status
    assert any(call[:3] == ["ps", "-o", "lstart="] for call in runner.calls)


def test_uninstalled_status_and_uninstall_are_safe(home: Path) -> None:
    runner = FakeRunner()
    manager = ServiceManager(system="Linux", runner=runner)
    status = manager.status()
    assert "not installed" in status
    assert "Version" in status and "Config:" in status and "Dashboard:" in status
    manager.uninstall()
    assert not runner.calls


def test_failed_uninstall_retains_registration(home: Path, definition: ServiceDefinition) -> None:
    runner = FakeRunner()
    manager = ServiceManager(system="Linux", runner=runner)
    manager.install(definition)
    runner.returncode = 1
    with pytest.raises(ServiceError):
        manager.uninstall()
    assert manager.unit_path.exists()
    assert manager.metadata_path.exists()


def test_missing_user_session_is_actionable_and_sanitized(home: Path, definition: ServiceDefinition) -> None:
    runner = FakeRunner()
    runner.returncode = 1
    runner.output = SECRET
    manager = ServiceManager(system="Linux", runner=runner)
    with pytest.raises(ServiceError, match="user session") as error:
        manager.install(definition)
    assert SECRET not in str(error.value)


def test_unsupported_platform_is_actionable(home: Path) -> None:
    with pytest.raises(ServiceError, match="macOS and Linux"):
        ServiceManager(system="Windows", runner=FakeRunner())


@pytest.mark.parametrize("action", ["install", "start", "stop", "restart", "status", "logs", "uninstall"])
def test_cli_dispatches_without_starting_daemon(monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    run = MagicMock(return_value=0)
    monkeypatch.setattr(cli, "run_service", run)
    monkeypatch.setattr("sys.argv", ["coordinare", "service", action])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 0
    assert run.call_args.args[0].service_action == action


def test_missing_service_action_prints_help(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["coordinare", "service"])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 2


def test_installer_defaults_to_issue_195_paths_and_interpreter_entrypoint(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    interpreter = home / "installed tool/bin/python"
    entrypoint = interpreter.with_name("coordinare")
    entrypoint.parent.mkdir(parents=True)
    entrypoint.touch()
    monkeypatch.setattr(sys, "executable", str(interpreter))
    manager = MagicMock()
    service_cli.install_service(manager=manager)
    saved = manager.install.call_args.args[0]
    assert saved.command == (str(entrypoint),)
    assert saved.config == home / "config home/coordinare/config.yaml"
    assert saved.env == saved.config.parent / "coordinare.env"
    assert saved.working_directory == saved.config.parent


def test_installer_missing_entrypoint_is_actionable(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "executable", str(home / "missing/bin/python"))
    monkeypatch.setattr(service_cli.shutil, "which", lambda name: None)
    with pytest.raises(ServiceError, match="Install coordinare"):
        service_cli.install_service(manager=MagicMock())


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_missing_config_fails_before_manager_commands(home: Path, definition: ServiceDefinition, system: str) -> None:
    runner = FakeRunner()
    manager = ServiceManager(system=system, runner=runner)
    definition.config.unlink()
    with pytest.raises(ServiceError, match="coordinare init"):
        manager.install(definition)
    assert not runner.calls and not manager.unit_path.exists()


def test_stopped_mac_service_can_start_and_restart(home: Path, definition: ServiceDefinition) -> None:
    runner = FakeRunner()
    manager = ServiceManager(system="Darwin", runner=runner)
    manager.install(definition)
    runner.calls.clear()
    runner.returncode = 1
    assert "not running" in manager.status()

    def unregistered(argv: list[str], *, capture: bool = True) -> subprocess.CompletedProcess[str]:
        runner.calls.append(argv)
        return subprocess.CompletedProcess(argv, int(argv[1] == "print"), "", "")

    manager.runner = unregistered
    manager.stop()
    manager.start()
    manager.restart()
    assert sum("bootstrap" in call for call in runner.calls) == 2


def test_status_queries_are_bounded_and_failed_session_is_not_reported_stopped(
    home: Path, definition: ServiceDefinition,
) -> None:
    runner = FakeRunner()
    manager = ServiceManager(system="Linux", runner=runner)
    manager.install(definition)
    runner.calls.clear()
    manager.status()
    assert len(runner.calls) == 1
    runner.returncode = 1
    with pytest.raises(ServiceError, match="user session"):
        manager.status()


def test_invalid_metadata_is_actionable(home: Path, definition: ServiceDefinition) -> None:
    manager = ServiceManager(system="Linux", runner=FakeRunner())
    manager.install(definition)
    manager.metadata_path.write_text("invalid-json")
    with pytest.raises(ServiceError, match="Rerun coordinare service install"):
        manager.status()
    manager.uninstall()


@pytest.mark.parametrize("action", ["start", "stop", "restart", "logs"])
def test_uninstalled_actions_explain_how_to_install(home: Path, action: str) -> None:
    runner = FakeRunner()
    manager = ServiceManager(system="Linux", runner=runner)
    with pytest.raises(ServiceError, match="service install"):
        getattr(manager, action)()
    assert not runner.calls


def test_systemd_quoting_handles_double_quotes_and_backslashes(definition: ServiceDefinition) -> None:
    special = replace(definition, command=('/tmp/bin$x %n "quotes"/bin\\coordinare', '$HOME/%u'))
    content = render_unit(special)
    assert 'ExecStart="/tmp/bin$$x %%n \\"quotes\\"/bin\\\\coordinare" "$$HOME/%%u"' in content
    with pytest.raises(ValueError, match="single-line"):
        render_unit(replace(definition, env=Path("/tmp/env\ninjected")))


def test_wrapper_delivers_literal_paths_and_env_without_shell_injection(
    definition: ServiceDefinition, tmp_path: Path,
) -> None:
    receiver = tmp_path / "receive args.py"
    result = tmp_path / "result.json"
    receiver.write_text(
        "from __future__ import annotations\n"
        "import json, os, sys\nfrom pathlib import Path\n"
        "Path(sys.argv[1]).write_text(json.dumps([sys.argv[2:], os.environ['COORDINARE_GITHUB_TOKEN']]))\n",
    )
    definition.env.write_text("COORDINARE_GITHUB_TOKEN=" + shlex.quote("literal $(false) ' $HOME") + "\n")
    wrapper = tmp_path / "run.sh"
    wrapper.write_text(render_wrapper(replace(definition, command=(sys.executable, str(receiver), str(result)))))
    completed = subprocess.run(["/bin/sh", str(wrapper)], check=False, capture_output=True, text=True)
    assert completed.returncode == 0
    assert json.loads(result.read_text()) == [["--config", str(definition.config)], "literal $(false) ' $HOME"]


def test_dashboard_url_expands_env_handles_multisymphony_and_ipv6(definition: ServiceDefinition) -> None:
    definition.config.write_text("global_config:\n  dashboard_host: '::1'\n  dashboard_port: ${SERVICE_196_PORT}\n")
    definition.env.write_text("SERVICE_196_PORT=8124\n")
    assert dashboard_url(definition) == "http://[::1]:8124"
    definition.config.write_text("[]")
    assert "unavailable" in dashboard_url(definition)


def test_runner_uses_resolved_argv_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    process = MagicMock(returncode=0)
    process.communicate = AsyncMock(return_value=(b"active\n", b""))
    create = AsyncMock(return_value=process)
    timeout = MagicMock(wraps=asyncio.timeout)
    monkeypatch.setattr("coordinare.service_manager.shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("coordinare.service_manager.asyncio.create_subprocess_exec", create)
    monkeypatch.setattr("coordinare.service_manager.asyncio.timeout", timeout)
    assert run_command(["systemctl", "--user", "show"]).stdout == "active\n"
    assert create.call_args.args == ("/usr/bin/systemctl", "--user", "show")
    timeout.assert_called_once_with(COMMAND_TIMEOUT_SECONDS)
    timeout.reset_mock()
    run_command(["journalctl", "--user"], capture=False)
    timeout.assert_called_once_with(None)


def test_runner_timeout_reaps_process_and_sanitizes_error(monkeypatch: pytest.MonkeyPatch) -> None:
    process = MagicMock(returncode=None)
    process.communicate = AsyncMock(side_effect=TimeoutError(SECRET))
    process.wait = AsyncMock()
    monkeypatch.setattr("coordinare.service_manager.shutil.which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr("coordinare.service_manager.asyncio.create_subprocess_exec", AsyncMock(return_value=process))
    with pytest.raises(ServiceError) as error:
        run_command(["systemctl", "--user", "status"])
    assert SECRET not in str(error.value)
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()


def test_runner_missing_binary_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("coordinare.service_manager.shutil.which", lambda name: None)
    with pytest.raises(ServiceError, match="unavailable"):
        run_command(["systemctl", "--user", "status"])


def test_developer_adapter_preserves_checkout_env_and_flags(
    home: Path, definition: ServiceDefinition, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = MagicMock()
    monkeypatch.setattr(service_dev, "ServiceManager", lambda: manager)
    monkeypatch.setattr(sys, "argv", ["service_dev", "--config", str(definition.config), "--env", str(definition.env), "--log-level", "debug"])
    service_dev.main()
    saved = manager.install.call_args.args[0]
    assert saved.command == (sys.executable, "-m", "coordinare", "--log-level", "debug")
    assert saved.env == definition.env
    assert saved.config == definition.config
    assert saved.working_directory / "src" == Path(saved.pythonpath)


def test_service_cli_reports_actionable_errors_without_manager_output(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    manager = MagicMock()
    manager.stop.side_effect = ServiceError("Run coordinare service install first.")
    monkeypatch.setattr(service_cli, "ServiceManager", lambda: manager)
    assert service_cli.run_service(argparse.Namespace(service_action="stop")) == 1
    assert "service install" in capsys.readouterr().err
    manager.stop.side_effect = OSError(SECRET)
    assert service_cli.run_service(argparse.Namespace(service_action="stop")) == 1
    assert SECRET not in capsys.readouterr().err


def test_log_interrupt_returns_cleanly(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = MagicMock()
    manager.logs.side_effect = KeyboardInterrupt
    monkeypatch.setattr(service_cli, "ServiceManager", lambda: manager)
    assert service_cli.run_service(argparse.Namespace(service_action="logs")) == 0


def test_install_refuses_registration_symlinks_that_could_overwrite_config(
    home: Path, definition: ServiceDefinition,
) -> None:
    manager = ServiceManager(system="Linux", runner=FakeRunner())
    manager.unit_path.parent.mkdir(parents=True)
    manager.unit_path.symlink_to(definition.config)
    original = definition.config.read_bytes()
    with pytest.raises(ServiceError, match="symbolic links"):
        manager.install(definition)
    assert definition.config.read_bytes() == original


def test_plist_serialization_escapes_xml_paths(definition: ServiceDefinition, tmp_path: Path) -> None:
    wrapper = tmp_path / "file <with & xml>.sh"
    content = render_plist(definition, wrapper, tmp_path / "log")
    assert b"&lt;with &amp; xml&gt;" in content
    assert plistlib.loads(content)["ProgramArguments"] == ["/bin/sh", str(wrapper)]


@pytest.fixture
def stop_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Stub process discovery so the real script cannot stop host processes."""
    root = tmp_path / "checkout"
    (root / "bin").mkdir(parents=True)
    script = root / "bin/stop"
    script.write_bytes((Path(__file__).parents[2] / "bin/stop").read_bytes())
    commands = tmp_path / "commands"
    commands.mkdir()
    trace = tmp_path / "trace"
    for name, body in {
        "uv": 'echo "service $*" >> "$TRACE"\nexit 7',
        "lsof": 'echo "port $*" >> "$TRACE"\nexit 1',
        "pkill": 'echo "sweep $*" >> "$TRACE"\nexit 1',
    }.items():
        stub = commands / name
        stub.write_text("#!/bin/bash\n" + body + "\n")
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(commands) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TRACE", str(trace))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return script, trace


def test_stop_failure_warns_and_still_sweeps(stop_script: tuple[Path, Path], tmp_path: Path) -> None:
    unit = tmp_path / ".config/systemd/user/coordinare.service"
    unit.parent.mkdir(parents=True)
    unit.touch()
    script, trace = stop_script
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert "sweep -9 -f python -m coordinare" in trace.read_text()
    assert "warning" in result.stderr.lower()
    assert "service stop" in result.stderr


def test_stop_ignores_relative_xdg_config_home(
    stop_script: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative-config")
    unit = tmp_path / ".config/systemd/user/coordinare.service"
    unit.parent.mkdir(parents=True)
    unit.touch()
    script, trace = stop_script
    manager = ServiceManager(system="Linux", runner=FakeRunner())
    assert manager.unit_path == unit
    subprocess.run(["bash", str(script)], check=False, capture_output=True, text=True)
    assert "service run python -m coordinare service stop" in trace.read_text()


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_checkout_install_without_env_is_optional_and_explained(
    home: Path, definition: ServiceDefinition, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], system: str,
) -> None:
    definition.env.unlink()
    manager = ServiceManager(system=system, runner=FakeRunner())
    monkeypatch.setattr(service_dev, "ServiceManager", lambda: manager)
    monkeypatch.setattr(sys, "argv", ["service_dev", "--config", str(definition.config), "--env", str(definition.env)])
    service_dev.main()
    output = capsys.readouterr()
    assert "optional" in output.err.lower()
    assert str(definition.env) in output.err
    assert "coordinare init" not in output.err
    assert manager.unit_path.exists()
    saved = manager._definition()
    if system == "Linux":
        assert "EnvironmentFile=-" in render_unit(saved)
    else:
        receiver = home / "receive.py"
        received = home / "received"
        receiver.write_text("from pathlib import Path\nPath(" + repr(str(received)) + ").touch()\n")
        manager.wrapper_path.write_text(render_wrapper(replace(saved, command=(sys.executable, str(receiver)))))
        result = subprocess.run(["sh", str(manager.wrapper_path)], capture_output=True, text=True, check=False)
        assert result.returncode == 0
        assert received.exists()


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_installed_service_still_requires_env(home: Path, definition: ServiceDefinition, system: str) -> None:
    definition.env.unlink()
    manager = ServiceManager(system=system, runner=FakeRunner())
    with pytest.raises(ServiceError, match="coordinare init"):
        manager.install(definition)


@pytest.mark.parametrize("system", ["Linux", "Darwin"])
def test_uninstall_removes_orphans_without_unit(home: Path, definition: ServiceDefinition, system: str) -> None:
    runner = FakeRunner()
    manager = ServiceManager(system=system, runner=runner)
    manager.install(definition)
    manager.wrapper_path.write_text("orphan wrapper")
    manager.log_path.write_text("retain logs")
    manager.unit_path.unlink()
    runner.calls.clear()
    manager.uninstall()
    assert not manager.wrapper_path.exists()
    assert not manager.metadata_path.exists()
    assert manager.log_path.read_text() == "retain logs"
    assert definition.config.exists() and definition.env.exists()
    assert not runner.calls


def test_uninstalled_status_labels_current_version(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("coordinare.service_manager._coordinare_version", lambda: "2026.10.123")
    status = ServiceManager(system="Linux", runner=FakeRunner()).status()
    assert "Version (current): 2026.10.123" in status
    assert "Version (installed):" not in status


def test_failed_stop_names_full_command(home: Path, definition: ServiceDefinition) -> None:
    runner = FakeRunner()
    manager = ServiceManager(system="Linux", runner=runner)
    manager.install(definition)
    runner.returncode = 1
    runner.output = SECRET
    with pytest.raises(ServiceError, match=r"systemctl --user stop coordinare\.service") as error:
        manager.stop()
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("session", ["running", "starting", "unreachable", "degraded"])
def test_linux_ci_requires_reachable_user_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session: str,
) -> None:
    root = Path(__file__).parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/service.yml").read_text())
    steps = workflow["jobs"]["lifecycle"]["steps"]
    setup = next(step for step in steps if step.get("id") == "session")
    commands = tmp_path / "commands"
    commands.mkdir()
    trace = tmp_path / "trace"
    for name, body in {
        "sudo": 'echo "$*" >> "$TRACE"',
        "sleep": "exit 0",
        "systemctl": (
            'echo "$*" >> "$TRACE"\n'
            'case "$SESSION" in\n'
            'running) echo running;;\n'
            'degraded) echo degraded; exit 1;;\n'
            'starting) if [[ -f "$READY" ]]; then echo running; else touch "$READY"; echo starting; exit 1; fi;;\n'
            '*) exit 1;;\nesac'
        ),
    }.items():
        stub = commands / name
        stub.write_text("#!/bin/bash\n" + body + "\n")
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(commands) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("USER", "service-runner")
    monkeypatch.setenv("TRACE", str(trace))
    monkeypatch.setenv("SESSION", session)
    monkeypatch.setenv("READY", str(tmp_path / "ready"))
    github_env = tmp_path / "github-env"
    monkeypatch.setenv("GITHUB_ENV", str(github_env))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "github-output"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    result = subprocess.run(["bash", "-euo", "pipefail", "-c", setup["run"]], capture_output=True, text=True, check=False)
    if session == "unreachable":
        assert result.returncode != 0
        assert "::error::" in result.stdout + result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert "loginctl enable-linger service-runner" in trace.read_text()
        assert "--user is-system-running" in trace.read_text()
        assert f"XDG_RUNTIME_DIR=/run/user/{os.getuid()}" in github_env.read_text()
        assert f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{os.getuid()}/bus" in github_env.read_text()
        if session == "starting":
            assert trace.read_text().count("--user is-system-running") >= 2
    lifecycle = next(step for step in steps if "scripts/service_smoke.py" in step.get("run", ""))
    assert "if" not in lifecycle
    gates = yaml.safe_load((root / ".github/workflows/pr-ci.yml").read_text())
    assert "lifecycle" not in gates["jobs"]["build-success"]["needs"]
