from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from coordinare.auth import build_auth
from coordinare.dashboard.app import check_port_available
from coordinare.doctor import CheckResult, DoctorReport, Status, run_checks
from coordinare.lifecycle import CANONICAL_ORDER
from coordinare.presets import HARNESS_COMMANDS
from coordinare.services.github import BOARD_COLUMN_TO_STATUS, GitHubService
from coordinare.start_checks import validate_start_time
from coordinare.transport.base import TransportError
from coordinare.transport.subprocess_transport import SubprocessTransport

if TYPE_CHECKING:
    from coordinare.config import CoordinareConfiguration, ProjectConfiguration

PROBE_TIMEOUT_SECONDS = 5.0
HTTP_OK = 200
REQUIRED_SCOPES = {"repo", "project", "read:org"}
REQUIRED_COLUMNS = {"TODO", "IN_PROGRESS", "IN_REVIEW", "BLOCKED", "DONE"}


@dataclass(frozen=True)
class ProbeOptions:
    stdin: str | None = None
    env: dict[str, str] | None = None
    timeout: float = PROBE_TIMEOUT_SECONDS


def check_directories(config: ProjectConfiguration) -> list[CheckResult]:
    """Actually create a temporary file, rather than trusting permission bits."""
    paths = {"state_file_path": config.state_file_path.expanduser().parent}
    if config.performer_log_dir is not None:
        paths["performer_log_dir"] = config.performer_log_dir.expanduser()
    for setting, log_config in (("transcripts", config.transcripts), ("run_logs", config.run_logs)):
        if log_config.enabled:
            paths[setting + ".dir"] = (
                log_config.directory
                or config.state_file_path.expanduser().parent / setting.replace("_", "-")
            ).expanduser()
    results = []
    for setting, path in paths.items():
        try:
            path.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=path):
                pass
            if setting == "state_file_path" and config.state_file_path.expanduser().exists():
                with config.state_file_path.expanduser().open("ab"):
                    pass
        except OSError:
            results.append(
                CheckResult(
                    setting,
                    Status.FAIL,
                    f"Cannot write the directory or file configured by {setting}.",
                    f"Set {setting} to a writable path outside performer mounts, or fix its ownership and permissions.",
                ),
            )
        else:
            results.append(CheckResult(setting, Status.OK, f"{setting} is writable."))
    return results


def check_port(config: ProjectConfiguration) -> CheckResult:
    try:
        check_port_available(config.dashboard_host, config.dashboard_port, label="dashboard")
    except (SystemExit, OSError):
        return CheckResult(
            "dashboard port",
            Status.FAIL,
            f"Dashboard cannot bind port {config.dashboard_port}.",
            "Stop the process using that port, or change dashboard_port and dashboard_host in config.yaml.",
        )
    return CheckResult("dashboard port", Status.OK, f"Port {config.dashboard_port} is available.")


def _token_and_repo(config: ProjectConfiguration, token: str) -> list[CheckResult]:
    results = []
    try:
        with httpx.Client(
            timeout=PROBE_TIMEOUT_SECONDS, headers={"Authorization": f"Bearer {token}"},
        ) as client:
            # Installation tokens have no /user identity; /installation/repositories validates them.
            identity = "/installation/repositories" if config.github_auth == "app" else "/user"
            response = client.get(config.github_api_url.rstrip("/") + identity)
            if response.status_code != HTTP_OK:
                return [
                    CheckResult(
                        "GitHub token",
                        Status.FAIL,
                        "GitHub rejected the token or could not validate its identity.",
                        "Set COORDINARE_GITHUB_TOKEN to a valid token; for App auth check github_app_id, github_installation_id and github_private_key_path.",
                    ),
                ]
            results.append(
                CheckResult("GitHub token", Status.OK, "GitHub accepted the credential."),
            )
            scopes_header = response.headers.get("X-OAuth-Scopes")
            if scopes_header is not None:
                scopes = {scope.strip() for scope in scopes_header.split(",")}
                if "admin:org" in scopes or "write:org" in scopes:
                    scopes.add("read:org")
                missing = REQUIRED_SCOPES - scopes
                if missing:
                    results.append(
                        CheckResult(
                            "GitHub scopes",
                            Status.FAIL,
                            f"Token is missing scopes: {', '.join(sorted(missing))}.",
                            "Grant repo, project and read:org scopes to the token in COORDINARE_GITHUB_TOKEN and authorize it for your organization's SSO if required.",
                        ),
                    )
                else:
                    results.append(
                        CheckResult(
                            "GitHub scopes",
                            Status.OK,
                            "repo, project and read:org scopes are present.",
                        ),
                    )
            else:
                results.append(
                    CheckResult(
                        "GitHub permissions",
                        Status.WARN,
                        "GitHub did not return classic token scopes. Read access is probed below; write permissions cannot be proved without a write.",
                        "For a fine-grained token or App, grant repository Contents, Issues and Pull requests read/write, and organization Projects read/write.",
                    ),
                )
            if config.github_repo:
                response = client.get(
                    f"{config.github_api_url.rstrip('/')}/repos/{config.github_org}/{config.github_repo}",
                )
                permissions = (
                    response.json().get("permissions", {})
                    if response.status_code == HTTP_OK
                    else {}
                )
                if response.status_code != HTTP_OK or permissions.get("push") is False:
                    results.append(
                        CheckResult(
                            "GitHub repository",
                            Status.FAIL,
                            "The configured repository is inaccessible or the token cannot push to it.",
                            "Correct github_org and github_repo; grant the token repository Contents, Issues and Pull requests read/write access.",
                        ),
                    )
                else:
                    results.append(
                        CheckResult(
                            "GitHub repository",
                            Status.OK,
                            "The configured repository is reachable.",
                        ),
                    )
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        results.append(
            CheckResult(
                "GitHub API",
                Status.FAIL,
                "Could not reach or read the GitHub API.",
                "Check github_api_url and network access, then run coordinare doctor again.",
            ),
        )
    return results


async def check_github(config: ProjectConfiguration) -> list[CheckResult]:
    try:
        auth = build_auth(config)
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            token = await auth.get_token()
    except Exception:
        return [
            CheckResult(
                "GitHub credential",
                Status.FAIL,
                "Could not obtain a GitHub credential.",
                "Set COORDINARE_GITHUB_TOKEN, or correct github_auth and the github_app_id, github_installation_id and github_private_key_path settings.",
            ),
        ]
    results = await asyncio.to_thread(_token_and_repo, config, token)
    if any(result.status is Status.FAIL for result in results):
        return results
    github = GitHubService(
        config.github_org,
        config.github_project_number,
        endpoint=config.github_graphql_url,
        auth=auth,
    )
    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            await github.initialize()
        options = github.field_cache.get("status_option_ids", {})
        found = {BOARD_COLUMN_TO_STATUS.get(name) for name in options}
        missing = REQUIRED_COLUMNS - found
        if missing:
            results.append(
                CheckResult(
                    "board columns",
                    Status.FAIL,
                    f"The board is missing columns: {', '.join(sorted(missing))}.",
                    "In the board's Status field add To Do (or Ready), In Progress, In Review, Blocked and Done options.",
                ),
            )
        else:
            results.append(
                CheckResult(
                    "GitHub board",
                    Status.OK,
                    "Board is reachable and all lifecycle Status columns exist.",
                ),
            )
    except Exception:
        results.append(
            CheckResult(
                "GitHub board",
                Status.FAIL,
                "Could not read the project board and its Status field.",
                "Correct github_org, github_project_number and github_graphql_url; grant Projects access and add a Status field with To Do, In Progress, In Review, Blocked and Done.",
            ),
        )
    finally:
        await github.aclose()
    return results


def _command_probe(
    name: str, command: list[str], fix: str, options: ProbeOptions | None = None,
) -> CheckResult:
    options = options or ProbeOptions()
    try:
        process = subprocess.run(
            command,
            input=options.stdin,
            env=options.env,
            capture_output=True,
            text=True,
            timeout=options.timeout,
            check=False,
        )
        answered = process.returncode == 0
        if options.stdin is not None:
            response = SubprocessTransport._parse_response(
                process.stdout.encode(), one_shot=True, returncode=process.returncode,
            )
            answered = answered and response.status == "healthy"
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError, TransportError):
        answered = False
    if not answered:
        return CheckResult(
            name, Status.FAIL, f"{name} is missing, failed or did not answer in time.", fix,
        )
    return CheckResult(name, Status.OK, f"{name} answered.")


def _local_harnesses(config: ProjectConfiguration) -> list[CheckResult]:
    results = []
    executables = set()
    harnesses = set()
    http_roles = {
        role
        for endpoint in config.performer_endpoints
        if endpoint.mode != "subprocess"
        for role in endpoint.roles
    }
    for role in CANONICAL_ORDER:
        resolved = config.performers.resolved_role(role)
        if resolved is None or resolved.max_concurrency == 0:
            continue
        if resolved.transport not in (None, "subprocess"):
            continue
        executable = resolved.executable or config.agent_executable
        if not executable and role in http_roles:
            continue
        harnesses.add(resolved.backend.replace("-", "_").lower())
        executables.add(executable)
    if not executables and config.agent_executable and not http_roles:
        executables.add(config.agent_executable)
    for harness in sorted(harnesses):
        binary = HARNESS_COMMANDS.get(harness, harness)
        cli_executable = shutil.which(binary)
        fix = f"Install and authenticate {binary}, ensure it is on PATH, or correct performers.default.backend (harness for a preset)."
        if cli_executable is None:
            results.append(
                CheckResult(
                    f"harness {harness}", Status.FAIL, f"{binary} is not installed on PATH.", fix,
                ),
            )
        else:
            results.append(_command_probe(f"harness {harness}", [cli_executable, "--version"], fix))
    for executable in sorted(executables):
        transport = SubprocessTransport(executable, int(PROBE_TIMEOUT_SECONDS), config=config)
        results.append(
            _command_probe(
                "performer protocol",
                [executable],
                "Install the performer package with uv sync --project agent/performer, or set agent_executable to bin/run-performer in a checkout (never to a bare harness CLI).",
                options=ProbeOptions(
                    stdin=json.dumps({"action": "health"}) + "\n",
                    env=transport._build_subprocess_env(),
                ),
            ),
        )
    if not executables and not http_roles:
        results.append(
            CheckResult(
                "performer transport",
                Status.FAIL,
                "No local performer executable or HTTP performer is configured.",
                "Set preset: solo and harness, or configure agent_executable or performer_endpoints.",
            ),
        )
    return results


def _container_harnesses(config: ProjectConfiguration) -> list[CheckResult]:
    docker = shutil.which("docker")
    fix = "Install and start Docker, then build the performer image with make docker or set performer_endpoints[].image to an available image."
    if docker is None:
        return [
            CheckResult("container runtime", Status.FAIL, "Docker is not installed on PATH.", fix),
        ]
    result = _command_probe("container runtime", [docker, "info"], fix)
    if result.status is Status.FAIL:
        return [result]
    results = [result]
    for endpoint in config.performer_endpoints:
        if endpoint.mode != "ephemeral":
            continue
        image = endpoint.image or config.performer_image
        result = _command_probe(
            f"performer image {endpoint.id}", [docker, "image", "inspect", image], fix,
        )
        results.append(result)
        if result.status is Status.FAIL:
            continue
        harnesses = {
            resolved.backend.replace("-", "_").lower()
            for role in endpoint.roles
            if (resolved := config.performers.resolved_role(role)) is not None
            and resolved.max_concurrency > 0
        }
        if not harnesses:
            harnesses = {endpoint.env.get("BACKEND", "opencode").replace("-", "_").lower()}
        for harness in sorted(harnesses):
            name = "coordinare-preflight-" + uuid.uuid4().hex
            binary = HARNESS_COMMANDS.get(harness, harness)
            try:
                results.append(
                    _command_probe(
                        f"container harness {harness}",
                        [
                            docker,
                            "run",
                            "--rm",
                            "--name",
                            name,
                            "-e",
                            f"BACKEND={harness}",
                            image,
                            binary,
                            "--version",
                        ],
                        f"Check performer_endpoints[{endpoint.id}].image and its {binary} installation; run docker run --rm -e BACKEND={harness} {image} {binary} --version.",
                        options=ProbeOptions(timeout=endpoint.readiness_timeout_s),
                    ),
                )
            finally:
                _command_probe(
                    "probe cleanup",
                    [docker, "rm", "-f", name],
                    "Remove the preflight container with docker rm -f " + name,
                )
    return results


async def check_harnesses(config: ProjectConfiguration) -> list[CheckResult]:
    results = []
    if config.agent_transport in {"kubernetes", "ssh"}:
        results.append(
            CheckResult(
                "remote harness",
                Status.WARN,
                "Remote harness installation is checked by the remote performer at dispatch.",
            ),
        )
    else:
        results.extend(await asyncio.to_thread(_local_harnesses, config))
        if any(endpoint.mode == "ephemeral" for endpoint in config.performer_endpoints):
            results.extend(await asyncio.to_thread(_container_harnesses, config))
    results.extend(await _persistent_harnesses(config))
    return results


async def _persistent_harnesses(config: ProjectConfiguration) -> list[CheckResult]:
    results = []
    for endpoint in config.performer_endpoints:
        if endpoint.mode != "persistent" or endpoint.endpoint is None:
            continue
        try:
            headers = (
                {"Authorization": "Bearer " + endpoint.auth_token.get_secret_value()}
                if endpoint.auth_token
                else {}
            )
            response = await asyncio.to_thread(
                httpx.get,
                str(endpoint.endpoint).rstrip("/") + "/status",
                headers=headers,
                timeout=PROBE_TIMEOUT_SECONDS,
            )
            ok = response.status_code == HTTP_OK
        except httpx.HTTPError:
            ok = False
        results.append(
            CheckResult(
                f"performer {endpoint.id}",
                Status.OK if ok else Status.FAIL,
                "Performer status endpoint answered."
                if ok
                else "Performer status endpoint did not answer successfully.",
                None
                if ok
                else f"Start the performer and correct performer_endpoints[{endpoint.id}].endpoint and auth_token.",
            ),
        )
    return results


def check_native_models(config: ProjectConfiguration) -> list[CheckResult]:
    results = []
    for endpoint in config.endpoints:
        if not endpoint.is_native:
            continue
        if not endpoint.auth_env:
            results.append(
                CheckResult(
                    f"model endpoint {endpoint.name}",
                    Status.WARN,
                    "Native endpoint uses the harness's own login. Provider authentication will be checked by the harness on its first request.",
                ),
            )
            continue
        credential = os.environ.get(endpoint.auth_env or "", "")
        fix = f"Set {endpoint.auth_env} to a valid provider API key in the daemon environment, or correct endpoints[{endpoint.name}].auth_env."
        if not credential:
            results.append(
                CheckResult(
                    f"model endpoint {endpoint.name}",
                    Status.FAIL,
                    "The native model API credential is missing.",
                    fix,
                ),
            )
            continue
        if endpoint.kind == "anthropic":
            url = "https://api.anthropic.com/v1/models"
            headers = {"x-api-key": credential, "anthropic-version": "2023-06-01"}
        else:
            url = "https://api.openai.com/v1/models"
            headers = {"Authorization": f"Bearer {credential}"}
        try:
            response = httpx.get(url, headers=headers, timeout=PROBE_TIMEOUT_SECONDS)
            ok = response.status_code == HTTP_OK
        except httpx.HTTPError:
            ok = False
        results.append(
            CheckResult(
                f"model endpoint {endpoint.name}",
                Status.OK if ok else Status.FAIL,
                "Native model API answered with valid authentication."
                if ok
                else "Native model API is unreachable or rejected its credential.",
                None if ok else fix,
            ),
        )
    return results


async def run_preflight(
    config: ProjectConfiguration,
    coordinare_config: CoordinareConfiguration | None = None,
    raw: dict[str, Any] | None = None,
) -> DoctorReport:
    """Shared startup and doctor gate, also callable by init before writing a config."""
    report = DoctorReport()
    try:
        validate_start_time(config, coordinare_config, raw)
    except ValueError as exc:
        report.results.append(
            CheckResult(
                "startup configuration",
                Status.FAIL,
                str(exc),
                "Correct the setting named above and run coordinare config validate.",
            ),
        )
        return report
    report.results.extend(await asyncio.to_thread(check_directories, config))
    report.results.append(check_port(config))
    configs = [config]
    if coordinare_config is not None:
        configs = [s.effective_config(config) for s in coordinare_config.symphonies if s.enabled]
    for effective in configs:
        probes = _active_model_config(effective)
        report.results.extend((await asyncio.to_thread(run_checks, probes)).results)
        report.results.extend(await check_github(effective))
        report.results.extend(await check_harnesses(effective))
        report.results.extend(await asyncio.to_thread(check_native_models, probes))
    return report


def _active_model_config(config: ProjectConfiguration) -> ProjectConfiguration:
    modes = set()
    for role in (*CANONICAL_ORDER, "env_bootstrap"):
        resolved = config.performers.resolved_role(role)
        if resolved is not None and resolved.max_concurrency > 0 and resolved.mode is not None:
            modes.add(resolved.mode)
    models = {
        ref
        for mode in config.modes
        if mode.name in modes
        for ref in (mode.tool, mode.thinking, mode.classifier)
        if ref is not None
    }
    model_endpoints = [model for model in config.model_endpoints if model.name in models]
    endpoints = {model.endpoint for model in model_endpoints}
    return config.model_copy(
        update={
            "endpoints": [endpoint for endpoint in config.endpoints if endpoint.name in endpoints],
            "model_endpoints": model_endpoints,
        },
    )
