"""Global config write helpers (436): guard, validate and persist a YAML write."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog
import yaml
from fastapi.responses import JSONResponse

from coordinare.config import expand_config_text
from coordinare.config_descriptors import GLOBAL_EDITABLE_FIELDS
from coordinare.config_validation import validate_raw_config
from coordinare.services.config_write_service import (
    ConcurrencyConflictError,
    _validate_candidate,
    atomic_write_yaml,
    guard_concurrency,
    safe_failure_reason,
)

if TYPE_CHECKING:
    from pathlib import Path

_log = structlog.get_logger(__name__)

_global_cfg_editable = GLOBAL_EDITABLE_FIELDS


def _global_unguarded_refusal() -> JSONResponse:
    # 157: required, not merely accepted. Spec 156 logged these instead of
    # refusing them, because refusing is a contract change and nobody could
    # weigh it without knowing how often it happened. Every first-party
    # caller now sends one, so what remains is automation writing config with
    # no protection against overwriting a concurrent edit -- and silently
    # losing someone's change is worse than a loud failure a script can be
    # taught to handle.
    #
    # 428 rather than 400: the request is well-formed, and what is missing is
    # a precondition. RFC 6585 §3 exists for exactly this, and it tells a
    # caller *what* to do rather than only that they were wrong.
    _log.warning(
        "config.global_write_refused_unguarded",
        hint="no expected_hash sent; refusing rather than risking a lost edit",
    )
    return JSONResponse(
        {
            "error": (
                "expected_hash is required. GET /api/config/global returns the "
                "current version as an ETag; send it back as expected_hash so a "
                "concurrent edit is refused rather than overwritten."
            ),
            "precondition_required": True,
        },
        status_code=428,
    )


def _guard_global_hash(config_path: Any, expected_hash: Any) -> JSONResponse | None:
    """Concurrency-guard a global write, or return the structured refusal."""
    if not isinstance(expected_hash, str):
        return JSONResponse(
            {"error": "expected_hash must be a string"}, status_code=400,
        )
    # A client that read the hash from the ETag sends it back with its
    # quotes, and a client holding it from `new_hash` sends it bare. Both
    # are the same version, so both are accepted -- otherwise quoting the
    # header correctly would have made every save from the page 409.
    expected_hash = expected_hash.removeprefix("W/").strip('"')
    try:
        guard_concurrency(config_path, expected_hash)
    except ConcurrencyConflictError as err:
        return JSONResponse({"error": str(err), "conflict": True}, status_code=409)
    except OSError as exc:
        # Same contract as _version_refusal; see the comment there.
        # Path kept out of the body; see the comment in _version_refusal.
        _log.warning("config.global_write_refused_unreadable", error=str(exc))
        return JSONResponse(
            {
                "error": "Could not read the configuration file: "
                f"{safe_failure_reason(exc)}",
            },
            status_code=403,
        )
    return None


def candidate_text(loaded: dict[str, Any]) -> str:
    """The exact text a global save writes, and the text it is validated as (219 final check)."""
    return yaml.dump(loaded, default_flow_style=False, allow_unicode=True)


def _atomic_write_yaml(config_path: Path, loaded: dict[str, Any]) -> None:
    """Replace config_path with loaded content atomically, preserving mode."""
    atomic_write_yaml(config_path, loaded)


def _validate_global_config(
    loaded: dict[str, Any], *, original: dict[str, Any] | None = None,
) -> JSONResponse | None:
    """Validate the whole candidate config before anything is written (219 re-review).

    The candidate is validated as the daemon will load it: the text that would be written,
    with ``${VAR}`` expanded exactly as ``ProjectConfiguration.from_yaml`` expands it (219 final
    check). A save that would leave the file unloadable (an env-cache root that overlaps a
    performer volume, or a volume whose expanded path does) is refused with a 422 that names
    the problem, and nothing is written. Deprecation warnings do not block a save.

    225: the candidate must also pass the daemon's own start-time checks and the candidate
    validation every other write runs (dashboard bind and token, OIDC, App key file, and unexpanded
    ${VAR} secrets). A save that the daemon could not start with is refused here, so the global
    route, the config assistant's Apply and MCP put_global all get the same refusal.
    """
    daemon_view = yaml.safe_load(expand_config_text(candidate_text(loaded)))
    result = validate_raw_config(daemon_view if isinstance(daemon_view, dict) else {})
    if not result.passed:
        detail = "; ".join(f"{err.field_path}: {err.fix_hint}" for err in result.errors)
        return JSONResponse({"error": f"Validation failed: {detail}"}, status_code=422)
    start_errors = _validate_candidate(loaded, original=original)
    if start_errors:
        detail = "; ".join(err.message for err in start_errors)
        return JSONResponse({"error": f"Validation failed: {detail}"},
                            status_code=403 if start_errors[0].code == "forbidden" else 422)
    return None
