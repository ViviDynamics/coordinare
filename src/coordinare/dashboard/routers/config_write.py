"""Global config write helpers (436): guard, validate and persist a YAML write."""
from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

import structlog
from fastapi.responses import JSONResponse

from coordinare.config_descriptors import GLOBAL_EDITABLE_FIELDS

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
    from coordinare.services.config_write_service import (
        ConcurrencyConflictError,
        guard_concurrency,
    )

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
        from coordinare.services.config_write_service import safe_failure_reason

        _log.warning("config.global_write_refused_unreadable", error=str(exc))
        return JSONResponse(
            {
                "error": "Could not read the configuration file: "
                f"{safe_failure_reason(exc)}",
            },
            status_code=403,
        )
    return None


def _atomic_write_yaml(config_path: Path, loaded: dict[str, Any]) -> None:
    """Replace config_path with loaded content atomically, preserving mode."""
    import os
    import stat
    import tempfile

    import yaml

    dir_ = config_path.parent
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".yaml.tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            yaml.dump(loaded, fh, default_flow_style=False, allow_unicode=True)
        original_mode = stat.S_IMODE(os.stat(config_path).st_mode)
        os.chmod(tmp, original_mode)
        os.replace(tmp, config_path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _validate_global_config(loaded: dict[str, Any]) -> JSONResponse | None:
    # Validate by constructing a throwaway config. All editable fields are
    # included even if they happen to be lists/dicts (e.g. human_reviewers);
    # other nested sections (performers, symphonies, …) that ProjectConfiguration
    # doesn't accept are stripped out so pydantic doesn't reject them.
    try:
        from coordinare.config import ProjectConfiguration
        ProjectConfiguration(**{
            kk: vv for kk, vv in loaded.items()
            if not isinstance(vv, (dict, list)) or kk in _global_cfg_editable
        })
    except Exception as exc:
        return JSONResponse({"error": f"Validation failed: {exc}"}, status_code=400)
    return None
