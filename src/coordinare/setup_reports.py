from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from coordinare.config import ProjectConfiguration
from coordinare.config_validation import ErrorType
from coordinare.doctor import Status

if TYPE_CHECKING:
    from coordinare.config_validation import ConfigValidationResult
    from coordinare.doctor import DoctorReport

SCHEMA_VERSION = 1
CONFIG_REFERENCE = "See config.example.yaml and docs/setup-for-agents.md."

# Curated text is intentional: probe details, config paths, unknown keys and
# validation exceptions can all contain credentials. None enter this protocol.
# Match longest prefixes first so model endpoint and container harness retain
# their own categories. IDs are a versioned public interface.
DOCTOR_CHECKS = {
    "startup configuration": ("startup", "Startup configuration requirements.", "Correct startup settings using the config reference."),
    "state_file_path": ("state_directory", "State storage writability.", "Set state_file_path to a writable location outside performer mounts."),
    "performer_log_dir": ("performer_directory", "Performer log storage writability.", "Set performer_log_dir to a writable location."),
    "transcripts.dir": ("transcript_directory", "Transcript storage writability.", "Set transcripts.dir to a writable location outside performer mounts."),
    "run_logs.dir": ("run_log_directory", "Run log storage writability.", "Set run_logs.dir to a writable location outside performer mounts."),
    "dashboard port": ("dashboard_port", "Dashboard port availability.", "Stop the existing daemon or choose an available dashboard_port; keep dashboard_host on loopback."),
    "dashboard binding": ("dashboard_binding", "Dashboard network binding.", "Set dashboard_host to 127.0.0.1."),
    "performer version ": ("performer_version", "Performer image and daemon version agreement.", "Rerun init with --force --run-mode docker after upgrading; retain your answers and credentials."),
    "endpoint '": ("endpoint", "Model endpoint /models or Ollama /api/tags verification; native providers are skipped.", "Verify GET {base_url}/models; base_url usually ends in /v1. Ollama without /v1 uses {base_url}/api/tags. Start the model service or correct endpoints[].base_url; containers need a container-reachable host address."),
    "model '": ("model", "Configured model availability; enumeration may be unavailable.", "Load the configured model (ollama pull <model> for Ollama) or correct model_endpoints[].model using the served ids shown by prose doctor."),
    "GitHub credential": ("github_credential", "GitHub credential acquisition.", "Supply COORDINARE_GITHUB_TOKEN or correct the configured GitHub App settings."),
    "GitHub token": ("github_token", "GitHub credential acceptance.", "Ask the operator to supply a valid authorized credential privately."),
    "GitHub scopes": ("github_scopes", "Classic GitHub token permissions.", "Ask the operator to review repo, project and read:org permissions and organization SSO; never widen scopes automatically."),
    "GitHub permissions": ("github_permissions", "Fine-grained or App write access needs operator verification.", "Ask the operator to verify repository Contents, Issues and Pull requests read/write and organization Projects read/write."),
    "GitHub repository": ("github_repository", "Configured repository access.", "Correct github_org and github_repo and ask the operator to verify repository access."),
    "GitHub API": ("github_api", "GitHub API availability.", "Check github_api_url and network access, then retry doctor."),
    "board columns": ("board_columns", "Required board Status columns.", "Add To Do or Ready, In Progress, In Review, Blocked and Done to the board Status field."),
    "GitHub board": ("github_board", "Project board access and lifecycle columns.", "Correct github_org and github_project_number; ask the operator to verify Projects access and Status columns."),
    "container harness ": ("container_harness", "Container harness readiness.", "Check the selected performer image includes the configured harness; rerun init in Docker mode."),
    "harness ": ("harness", "Local harness installation and readiness.", "Install and authenticate the selected harness CLI on PATH."),
    "performer protocol": ("performer_protocol", "Local performer protocol readiness.", "Reinstall the matching release with its bundled performer; agent_executable must be the performer protocol command."),
    "performer transport": ("performer_transport", "Performer transport configuration.", "Use init to select local or docker mode, or configure performer_endpoints."),
    "container runtime": ("container_runtime", "Docker runtime readiness.", "Install and start Docker, then retry doctor."),
    "performer image ": ("performer_image", "Local availability of the performer image.", "Rerun init with --force --run-mode docker to pull and pin the release image."),
    "remote harness": ("remote_harness", "Remote harness readiness is checked at dispatch.", "Verify the remote performer's harness installation before dispatching work."),
    "performer ": ("performer", "Persistent performer status endpoint.", "Start the performer and correct performer_endpoints[].endpoint and auth_token."),
    "model endpoint ": ("native_model", "Native provider authentication and reachability.", "Supply the API key named by endpoints[].auth_env privately, or repair provider connectivity; harness login is verified on first use."),
    "egress enforcement": ("egress", "Kubernetes NetworkPolicy egress enforcement.", "Verify cluster credentials, scheduling and CNI egress enforcement before relying on network isolation."),
}


def check(check_id: str, status: str, message: str, fix: str | None, *, field: str | None = None) -> dict[str, Any]:
    return {"id": check_id, "status": status, "message": message, "fix": fix, "field": field}


def report(command: str, checks: list[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    for item in checks:
        counts[item["id"]] += 1
        item["instance"] = counts[item["id"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "ok": not any(item["status"] == Status.FAIL for item in checks),
        "checks": checks,
    }


def safe_field(path: str) -> str | None:
    """Retain only a known top-level setting, never arbitrary input or dict keys."""
    parts = path.split(".")
    if parts[0] == "global_config":
        parts = parts[1:]
    first = parts[0].split("[")[0] if parts else ""
    if first in {"config_file", "symphonies"} or first in ProjectConfiguration.model_fields:
        return first
    return None


def validation_checks(result: ConfigValidationResult, *, strict: bool) -> list[dict[str, Any]]:
    checks = [check(
        "config.schema", Status.OK if result.passed else Status.FAIL,
        "Configuration schema validation.", None if result.passed else CONFIG_REFERENCE,
    )]
    checks.extend(
        check(
            "config." + error.error_type.value, Status.FAIL,
            "A configuration setting is missing, unknown or invalid.",
            (
                "Run coordinare init to create a config, or pass --config <path> to an existing file."
                if error.field_path == "config_file" and error.error_type is ErrorType.missing
                else "Correct the named field if present. " + CONFIG_REFERENCE
            ),
            field=safe_field(error.field_path),
        ) for error in result.errors
    )
    warning_status = Status.FAIL if strict else Status.WARN
    checks.extend(
        check(
            "config.deprecated", warning_status, "A deprecated configuration setting is present.",
            "Migrate the named field using the config reference.", field=safe_field(warning.field_name),
        ) for warning in result.warnings
    )
    checks.extend(
        check(
            "config.placeholder", warning_status, "An unset environment placeholder will be omitted.",
            "Supply the referenced environment variable privately or remove the unused setting.",
            field=safe_field(field),
        ) for field, _message in result.placeholder_warnings
    )
    return checks


def doctor_checks(result: DoctorReport) -> list[dict[str, Any]]:
    checks = []
    prefixes = sorted(DOCTOR_CHECKS, key=len, reverse=True)
    for item in result.results:
        category, message, fix = next(
            (DOCTOR_CHECKS[prefix] for prefix in prefixes if item.name.startswith(prefix)),
            ("other", "Additional preflight check.", CONFIG_REFERENCE),
        )
        checks.append(check(
            "doctor." + category, item.status.value, message,
            fix if item.status in {Status.FAIL, Status.WARN} else None,
        ))
    return checks
