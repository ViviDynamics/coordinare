from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "install.sh"
VERSION = "2026.10.84"
NEXT_VERSION = "2026.10.85"
LEGACY_TAG = "2026.09.01.3"
NORMALIZED_VERSION = "2026.9.1.3"
EXECUTABLE_MODE = 0o755
TIMEOUT_SECONDS = 30
PACKAGES = (
    "coordinare",
    "performer",
    "coordinare_ci_detection",
    "coordinare_routing_models",
    "coordinare_service_inference",
)

FAKE_UV = r"""#!/bin/sh
set -eu
printf '%s\n' "$@" >> "$HOME/uv.calls"
case "$1 $2" in
    'tool install')
        for arg do
            case "$arg" in
                */coordinare-*-py3-none-any.whl)
                    version=${arg##*/coordinare-}
                    version=${version%-py3-none-any.whl}
                    ;;
            esac
        done
        mkdir -p "$UV_TOOL_BIN_DIR" "$UV_TOOL_DIR/coordinare"
        printf '#!/bin/sh\nprintf "coordinare %s\\n"\n' "$version" > "$UV_TOOL_BIN_DIR/coordinare"
        printf '#!/bin/sh\nexit 0\n' > "$UV_TOOL_BIN_DIR/performer"
        chmod +x "$UV_TOOL_BIN_DIR/coordinare" "$UV_TOOL_BIN_DIR/performer"
        ;;
    'tool uninstall')
        rm -f "$UV_TOOL_BIN_DIR/coordinare" "$UV_TOOL_BIN_DIR/performer"
        rm -rf "$UV_TOOL_DIR/coordinare"
        ;;
    *) exit 1 ;;
esac
"""


@dataclass
class Release:
    root: Path
    home: Path
    directory: Path
    env: dict[str, str]

    def run(
        self, *args: str, shell: str = "sh", piped: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [shell, "-s", "--", *args] if piped else [shell, str(SCRIPT), *args]
        return subprocess.run(
            command,
            input=SCRIPT.read_text() if piped else "",
            env=self.env,
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
            timeout=TIMEOUT_SECONDS,
            start_new_session=True,
        )

    def calls(self) -> str:
        path = self.home / "uv.calls"
        return path.read_text() if path.exists() else ""


def write_release(directory: Path, version: str = VERSION) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    manifest = []
    for package in PACKAGES:
        name = f"{package}-{version}-py3-none-any.whl"
        content = f"wheel for {package} {version}".encode()
        (directory / name).write_bytes(content)
        manifest.append(f"{hashlib.sha256(content).hexdigest()}  {name}\n")
    (directory / "SHA256SUMS").write_text("".join(manifest))


@pytest.fixture
def release(tmp_path: Path) -> Release:
    home = tmp_path / "home with spaces"
    binaries = home / ".local/bin"
    binaries.mkdir(parents=True)
    (binaries / "uv").write_text(FAKE_UV)
    (binaries / "uv").chmod(EXECUTABLE_MODE)
    directory = tmp_path / "releases/download" / VERSION
    write_release(directory)
    latest = tmp_path / "releases/latest"
    latest.mkdir()
    (latest / "download").symlink_to(directory, target_is_directory=True)
    env = {
        "HOME": str(home),
        "PATH": f"{binaries}:{os.defpath}",
        "COORDINARE_RELEASE_ROOT": (tmp_path / "releases").as_uri(),
        "COORDINARE_YES": "1",
        "COORDINARE_NO_INIT": "1",
    }
    temporary = tmp_path / "temporary downloads"
    temporary.mkdir()
    env["TMPDIR"] = str(temporary)
    return Release(tmp_path, home, directory, env)


@pytest.mark.parametrize("shell", ["sh", "dash", "bash"])
def test_piped_latest_install_reports_version_and_next_step(release: Release, shell: str) -> None:
    result = release.run("--yes", shell=shell, piped=True)
    assert result.returncode == 0, result.stderr
    assert f"coordinare {VERSION}" in result.stdout
    assert "Next step: coordinare init" in result.stdout
    assert "Verified" in result.stdout
    assert "tool\ninstall\n" in release.calls()
    assert "--with-executables-from\nperformer\n" in release.calls()
    assert "--overrides" in release.calls()


@pytest.mark.parametrize(
    "corruption",
    ["daemon", "shared", "performer", "hash", "missing", "duplicate", "unsafe", "malformed"],
)
def test_corruption_aborts_before_any_uv_call(release: Release, corruption: str) -> None:
    manifest = release.directory / "SHA256SUMS"
    original = manifest.read_text()
    if corruption in {"daemon", "shared", "performer"}:
        package = {
            "daemon": "coordinare",
            "shared": "coordinare_ci_detection",
            "performer": "performer",
        }[corruption]
        (release.directory / f"{package}-{VERSION}-py3-none-any.whl").write_text("corrupt")
    elif corruption == "hash":
        manifest.write_text("0" * len(original.split()[0]) + original[len(original.split()[0]) :])
    elif corruption == "missing":
        manifest.write_text("\n".join(original.splitlines()[1:]) + "\n")
    elif corruption == "duplicate":
        manifest.write_text(original + original.splitlines()[0] + "\n")
    elif corruption == "unsafe":
        manifest.write_text(original + f"{hashlib.sha256(b'bad').hexdigest()}  ../escape\n")
    else:
        manifest.write_text(original + "invalid checksum\n")
    result = release.run()
    assert result.returncode != 0
    assert "checksum" in result.stderr.lower()
    assert not release.calls()
    assert not (release.home / ".local/bin/coordinare").exists()
    assert not list(Path(release.env["TMPDIR"]).iterdir())


@pytest.mark.parametrize("asset", ["SHA256SUMS", f"performer-{VERSION}-py3-none-any.whl"])
def test_missing_download_keeps_an_existing_install(release: Release, asset: str) -> None:
    assert release.run().returncode == 0
    calls = release.calls()
    installed = release.home / ".local/bin/coordinare"
    original = installed.read_bytes()
    (release.directory / asset).unlink()
    result = release.run()
    assert result.returncode != 0
    assert "Download failed" in result.stderr
    assert release.calls() == calls
    assert installed.read_bytes() == original
    assert not list(Path(release.env["TMPDIR"]).iterdir())


@pytest.mark.parametrize("pin_source", ["flag", "env", "precedence", "legacy"])
def test_pinned_release_is_used_instead_of_latest(release: Release, pin_source: str) -> None:
    args = []
    tag = LEGACY_TAG if pin_source == "legacy" else NEXT_VERSION
    version = NORMALIZED_VERSION if pin_source == "legacy" else NEXT_VERSION
    write_release(release.root / "releases/download" / tag, version)
    if pin_source == "env":
        release.env["COORDINARE_VERSION"] = tag
    else:
        args = ["--version", tag]
        release.env["COORDINARE_VERSION"] = "invalid"
    result = release.run(*args)
    assert result.returncode == 0, result.stderr
    assert f"coordinare {version}" in result.stdout


def test_rerun_and_upgrade_preserve_configuration_and_state(release: Release) -> None:
    config = release.home / ".coordinare/config.yaml"
    config.parent.mkdir()
    config.write_text("secret: keep me\n")
    state = release.root / "coordinare.state.json"
    state.write_text('{"keep": true}')
    for _ in range(2):
        result = release.run()
        assert result.returncode == 0, result.stderr
    write_release(release.root / "releases/download" / NEXT_VERSION, NEXT_VERSION)
    result = release.run("--version", NEXT_VERSION)
    assert result.returncode == 0, result.stderr
    assert NEXT_VERSION in result.stdout
    assert config.read_text() == "secret: keep me\n"
    assert state.read_text() == '{"keep": true}'
    assert "keep me" not in result.stdout + result.stderr
    assert "--force" in release.calls()


@pytest.mark.parametrize("purge", [False, True])
def test_uninstall_preserves_data_unless_purge_requested(release: Release, purge: bool) -> None:
    assert release.run().returncode == 0
    data = release.home / ".coordinare"
    data.mkdir()
    (data / "config.yaml").write_text("private config")
    (data / "state.json").write_text("private state")
    result = release.run("--uninstall", *(["--purge"] if purge else []))
    assert result.returncode == 0, result.stderr
    assert data.exists() is not purge
    assert not (release.home / ".local/bin/coordinare").exists()
    assert "private config" not in result.stdout + result.stderr
    assert "private state" not in result.stdout + result.stderr
    assert release.run("--uninstall").returncode == 0


def test_path_instruction_when_user_bin_is_absent(release: Release) -> None:
    release.env["PATH"] = os.defpath
    result = release.run()
    assert result.returncode == 0, result.stderr
    assert 'export PATH="$HOME/.local/bin:$PATH"' in result.stdout


def test_unattended_environment_flags_and_storage_overrides(release: Release) -> None:
    external = release.root / "outside-home"
    release.env.update(
        COORDINARE_NO_INIT="true",
        COORDINARE_YES="yes",
        UV_TOOL_DIR=str(external),
        UV_TOOL_BIN_DIR=str(external),
        UV_CACHE_DIR=str(external),
        UV_PYTHON_INSTALL_DIR=str(external),
    )
    assert release.run(piped=True).returncode == 0
    assert not external.exists()
    data = release.home / ".coordinare"
    data.mkdir()
    (data / "state.json").write_text("keep this state")
    release.env.update(COORDINARE_UNINSTALL="true", COORDINARE_PURGE="yes")
    assert release.run(piped=True).returncode == 0
    assert not data.exists()


@pytest.mark.parametrize(
    "setting", ["COORDINARE_YES", "COORDINARE_NO_INIT", "COORDINARE_UNINSTALL", "COORDINARE_PURGE"],
)
def test_invalid_boolean_settings_are_rejected(release: Release, setting: str) -> None:
    release.env[setting] = "maybe"
    result = release.run()
    assert result.returncode != 0
    assert "Boolean" in result.stderr
    assert not release.calls()


@pytest.mark.parametrize(
    "args", [["--purge"], ["--version"], ["--version", "../bad"], ["--unknown"]],
)
def test_invalid_flags_fail_without_mutation(release: Release, args: list[str]) -> None:
    assert release.run(*args).returncode != 0
    assert not release.calls()


def test_missing_uv_without_terminal_fails_with_instructions(release: Release) -> None:
    (release.home / ".local/bin/uv").unlink()
    release.env["COORDINARE_YES"] = "0"
    result = release.run(piped=True)
    assert result.returncode != 0
    assert "--yes" in result.stderr
    assert not release.calls()


@pytest.mark.parametrize("corrupt", [False, True])
def test_missing_uv_bootstrap_requires_verified_wheels(release: Release, corrupt: bool) -> None:
    binaries = release.home / ".local/bin"
    uv_source = release.root / "uv-source"
    (binaries / "uv").rename(uv_source)
    bootstrap = release.root / "bootstrap.sh"
    bootstrap.write_text(
        "#!/bin/sh\nset -eu\n"
        'test "$UV_UNMANAGED_INSTALL" = "$HOME/.local/bin"\n'
        'cp "$FAKE_UV_SOURCE" "$UV_UNMANAGED_INSTALL/uv"\n',
    )
    curl = shutil.which("curl")
    assert curl is not None
    wrapper = binaries / "curl"
    wrapper.write_text(
        '#!/bin/sh\ncase "$*" in\n*https://astral.sh/uv/install.sh*)\n'
        '  previous=""\n  for argument do\n'
        '    if [ "$previous" = --output ]; then output=$argument; fi\n'
        "    previous=$argument\n  done\n"
        '  cp "$FAKE_BOOTSTRAP_SCRIPT" "$output"\n'
        '  echo bootstrap >> "$HOME/bootstrap.calls" ;;\n'
        f'*) exec "{curl}" "$@" ;;\nesac\n',
    )
    wrapper.chmod(EXECUTABLE_MODE)
    release.env.update(FAKE_UV_SOURCE=str(uv_source), FAKE_BOOTSTRAP_SCRIPT=str(bootstrap))
    if corrupt:
        (release.directory / f"coordinare-{VERSION}-py3-none-any.whl").write_text("corrupt")
    result = release.run("--yes", piped=True)
    assert (result.returncode == 0) is not corrupt, result.stderr
    assert (release.home / "bootstrap.calls").exists() is not corrupt
    assert bool(release.calls()) is not corrupt


def write_real_wheels(directory: Path) -> None:
    """Minimal valid wheels test the real uv resolver without external downloads."""
    for package in PACKAGES:
        metadata = (
            f"Metadata-Version: 2.3\nName: {package}\nVersion: {VERSION}\nRequires-Python: >=3.12\n"
        )
        if package in {"coordinare", "performer"}:
            for shared in PACKAGES[2:]:
                metadata += f"Requires-Dist: {shared} @ https://invalid.example/{shared}-{VERSION}-py3-none-any.whl\n"
        distribution = f"{package}-{VERSION}.dist-info"
        with zipfile.ZipFile(directory / f"{package}-{VERSION}-py3-none-any.whl", "w") as wheel:
            wheel.writestr(distribution + "/METADATA", metadata)
            wheel.writestr(
                distribution + "/WHEEL",
                "Wheel-Version: 1.0\nGenerator: installer-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
            wheel.writestr(distribution + "/RECORD", "")
            if package in {"coordinare", "performer"}:
                wheel.writestr(
                    distribution + "/entry_points.txt",
                    f"[console_scripts]\n{package} = {package}_stub:main\n",
                )
                wheel.writestr(
                    f"{package}_stub.py",
                    f'from __future__ import annotations\n\ndef main():\n    print("{package} {VERSION}")\n',
                )
    manifest = []
    for path in sorted(directory.glob("*.whl")):
        manifest.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n")
    (directory / "SHA256SUMS").write_text("".join(manifest))


def test_real_uv_installs_verified_shared_wheels_and_both_commands(release: Release) -> None:
    uv = shutil.which("uv")
    assert uv is not None, "The repository test environment requires uv."
    (release.home / ".local/bin/uv").unlink()
    # Preserve discovery of the test runner's Python, but keep all uv writes isolated.
    release.env["PATH"] = f"{Path(uv).parent}:{os.environ['PATH']}"
    release.env["UV_PYTHON_DOWNLOADS"] = "never"
    release.env["UV_OFFLINE"] = "1"
    release.env["UV_NO_INDEX"] = "1"
    write_real_wheels(release.directory)
    result = release.run("--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Installed coordinare {VERSION}" in result.stdout
    assert (release.home / ".local/bin/performer").is_file()
    assert release.run("--yes").returncode == 0
    assert release.run("--uninstall").returncode == 0
    assert not (release.home / ".local/bin/performer").exists()


@pytest.mark.parametrize(
    "system,arch",
    [
        ("Linux", "aarch64"),
        ("Darwin", "arm64"),
        ("Darwin", "x86_64"),
        ("Linux", "x86_64"),
        ("MINGW64_NT", "x86_64"),
        ("Linux", "riscv64"),
    ],
)
def test_platform_detection(release: Release, system: str, arch: str) -> None:
    uname = release.home / ".local/bin/uname"
    uname.write_text(f'#!/bin/sh\ncase "$1" in -s) echo {system} ;; -m) echo {arch} ;; esac\n')
    uname.chmod(EXECUTABLE_MODE)
    result = release.run()
    supported = system in {"Linux", "Darwin"} and arch != "riscv64"
    assert (result.returncode == 0) is supported, result.stderr
    if not supported:
        assert not release.calls()
        if system == "MINGW64_NT":
            assert "WSL" in result.stderr


def test_truncated_script_cannot_execute(release: Release) -> None:
    source = SCRIPT.read_text()
    result = subprocess.run(
        ["sh"],
        input=source[: source.rindex(")")],
        env=release.env,
        capture_output=True,
        text=True,
        check=False,
        timeout=TIMEOUT_SECONDS,
    )
    assert result.returncode != 0
    assert not release.calls()


def test_shellcheck_when_available() -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck:
        result = subprocess.run(
            [shellcheck, "-s", "sh", str(SCRIPT)], capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_real_installer_smoke_gates_release_publication() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/main-branch-build.yml").read_text())["jobs"]
    smoke = jobs["installer-smoke"]
    assert set(smoke["strategy"]["matrix"]["os"]) == {"ubuntu-latest", "macos-latest"}
    assert "release-artifacts" in smoke["needs"]
    assert "installer-smoke" in jobs["publish-artifacts"]["needs"]
    commands = "\n".join(step.get("run", "") for step in smoke["steps"])
    assert "install.sh" in commands
    assert "--version" in commands
    assert "performer" in commands


@pytest.fixture(params=["sha256sum", "shasum"])
def workspace_uninstall_tools(release: Release, request: pytest.FixtureRequest) -> None:
    """Expose only required tools; Docker must be explicitly stubbed by each test."""
    tools = release.root / "uninstall-tools"
    tools.mkdir()
    for name in ("sh", "uname", "awk"):
        executable = shutil.which(name)
        assert executable is not None
        (tools / name).symlink_to(executable)
    # Exercise both checksum interfaces even on hosts that only ship one of them.
    checksum = tools / request.param
    arguments = ["-a", "256"] if request.param == "shasum" else []
    checksum.write_text(
        f"#!{sys.executable}\n"
        "import hashlib, sys\n"
        f"assert sys.argv[1:] == {arguments!r}\n"
        "print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest() + '  -')\n",
    )
    checksum.chmod(EXECUTABLE_MODE)
    release.env["PATH"] = f"{release.home / '.local/bin'}:{tools}"


@pytest.mark.usefixtures("workspace_uninstall_tools")
@pytest.mark.parametrize("consent,remove_status,removed", [(True, 0, True), (False, 0, False), (True, 1, True)])
def test_uninstall_workspace_volume_consent_and_nonfatal_failure(release: Release, consent: bool, remove_status: int, removed: bool) -> None:
    from coordinare.user_config import workspace_volume_name

    config = release.home / "custom config"
    config.mkdir()
    volume = workspace_volume_name(config)
    docker = release.home / ".local/bin/docker"
    docker.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/docker.calls"\n'
        'case "$1 $2" in\n'
        f'  "volume inspect") [ "$3" = "{volume}" ] ;;\n'
        f'  "volume rm") printf "volume in use\\n" >&2; exit {remove_status} ;;\n'
        '  *) exit 1 ;;\nesac\n',
    )
    docker.chmod(EXECUTABLE_MODE)
    release.env["COORDINARE_YES"] = "0"
    result = release.run("--uninstall", "--config-dir", str(config), *(["--yes"] if consent else []))
    assert result.returncode == 0, result.stderr
    calls = (release.home / "docker.calls").read_text()
    assert f"volume inspect {volume}" in calls
    assert (f"volume rm {volume}" in calls) is removed
    if remove_status:
        assert "volume in use" in result.stderr
    if not consent:
        assert "--yes" in result.stderr
    assert config.is_dir()


@pytest.mark.usefixtures("workspace_uninstall_tools")
@pytest.mark.parametrize("location", ["default", "xdg", "relative-xdg", "symlink"])
def test_uninstall_volume_name_matches_init_directory(release: Release, location: str) -> None:
    from coordinare.user_config import workspace_volume_name

    config = release.home / ".config/coordinare"
    arguments = []
    if location == "xdg":
        release.env["XDG_CONFIG_HOME"] = str(release.home / "xdg")
        config = release.home / "xdg/coordinare"
    elif location == "relative-xdg":
        release.env["XDG_CONFIG_HOME"] = "ignored-relative-root"
    config.mkdir(parents=True)
    if location == "symlink":
        alias = release.home / "config alias"
        alias.symlink_to(config, target_is_directory=True)
        arguments = ["--config-dir", str(alias)]
    volume = workspace_volume_name(config)
    docker = release.home / ".local/bin/docker"
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/docker.calls"\n')
    docker.chmod(EXECUTABLE_MODE)
    result = release.run("--uninstall", *arguments)
    assert result.returncode == 0, result.stderr
    assert (release.home / "docker.calls").read_text().splitlines() == [f"volume inspect {volume}", f"volume rm {volume}"]


@pytest.mark.usefixtures("workspace_uninstall_tools")
def test_uninstall_reports_missing_docker_without_losing_config(release: Release) -> None:
    config = release.home / ".config/coordinare"
    config.mkdir(parents=True)
    result = release.run("--uninstall", "--yes")
    assert result.returncode == 0, result.stderr
    assert "Docker is unavailable" in result.stderr
    assert config.is_dir()


@pytest.mark.usefixtures("workspace_uninstall_tools")
@pytest.mark.parametrize("answer,removed", [("yes", True), ("no", False)])
def test_uninstall_volume_interactive_confirmation(release: Release, answer: str, removed: bool) -> None:
    import pty

    config = release.home / ".config/coordinare"
    config.mkdir(parents=True)
    docker = release.home / ".local/bin/docker"
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$HOME/docker.calls"\n')
    docker.chmod(EXECUTABLE_MODE)
    release.env["COORDINARE_YES"] = "0"
    master, slave = pty.openpty()
    try:
        with subprocess.Popen(
            ["sh", str(SCRIPT), "--uninstall"], stdin=slave,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=release.root, env=release.env,
        ) as process:
            os.write(master, (answer + "\n").encode())
            stdout, stderr = process.communicate(timeout=TIMEOUT_SECONDS)
            assert process.returncode == 0, stdout + stderr
    finally:
        os.close(master)
        os.close(slave)
    assert "all its workspaces [no]" in stderr
    assert ("volume rm" in (release.home / "docker.calls").read_text()) is removed
