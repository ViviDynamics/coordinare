"""Symphony CRUD and bootstrap routes (436)."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog
import yaml
from fastapi import (  # noqa: TC002 — FastAPI resolves handler signatures at registration; runtime import required
    FastAPI,
    Request,
)
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError

from coordinare.config import UnknownPersonaRoleError
from coordinare.dashboard.guards import _version_headers, _version_refusal
from coordinare.dashboard.routers.personas import _session_actor
from coordinare.dashboard.secret_overrides import (
    _mask_secret_overrides,
    _restore_masked_secrets,
    _secret_override_fields,
)
from coordinare.services.config_documents import symphony_documents
from coordinare.services.config_write_service import _validate_candidate, atomic_write_yaml
from coordinare.services.env_bootstrap_control import request_env_bootstrap_reset
from coordinare.services.environment_attempt import EnvironmentDiscoveryError

if TYPE_CHECKING:
    from pathlib import Path

    from coordinare.dashboard.context import DashboardContext

_log = structlog.get_logger(__name__)


def _log_validation_failure(event: str, name: str, exc: Exception) -> None:
    """Log error metadata without exception text, source input, or traceback."""
    locations = []
    if isinstance(exc, PydanticValidationError):
        locations = [
            ".".join(str(part) for part in error["loc"])
            for error in exc.errors(include_input=False, include_context=False)
        ]
    _log.warning(event, name=name, error_type=type(exc).__name__, field_locations=locations)


def _persist_symphony_configs(sym_configs: dict[str, Any], config_path: Path | None) -> None:
    """Atomically write the current symphony list to config.yaml (FR-020)."""
    if config_path is None or not config_path.is_file():
        msg = "Config file not available; symphony changes are in-memory only"
        raise ValueError(msg)
    loaded = yaml.safe_load(config_path.read_text())
    if not isinstance(loaded, dict):
        msg = "Config file does not contain a YAML mapping"
        raise ValueError(msg)
    loaded["symphonies"] = symphony_documents(sym_configs, loaded)
    atomic_write_yaml(config_path, loaded)

def _register_get_symphonies(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon
        config_path = ctx.config_path

        @app.get("/api/symphonies")
        async def get_symphonies() -> JSONResponse:
            """List all symphonies with their current state (Task 9)."""
            symphony_configs = daemon.state.get("symphony_configs") or {}
            symphony_states = daemon.state.get("symphony_states") or {}
            config_version = daemon.state.get("config_version", 0)

            env_cache_states = daemon.state.get("env_cache") or {}
            symphonies = []
            for i, (name, cfg) in enumerate(symphony_configs.items()):
                state = symphony_states.get(name)
                # 077: bootstrap status for the at-a-glance overview badge.
                _ec = env_cache_states.get(name)
                symphonies.append({
                    "name": name,
                    "priority": i,
                    "github_project_number": getattr(cfg, "github_project_number", None),
                    "env_bootstrap_performer_id": getattr(cfg, "env_bootstrap_performer_id", None),
                    "bootstrap_in_flight": bool(getattr(_ec, "bootstrap_in_flight", False)) if _ec else False,
                    "cache_dir_ready": bool(getattr(_ec, "cache_dir_ready", False)) if _ec else False,
                    "last_bootstrap_succeeded": getattr(_ec, "last_bootstrap_succeeded", None) if _ec else None,
                    "last_bootstrap_error": getattr(_ec, "last_bootstrap_error", None) if _ec else None,
                    "cycle_count": getattr(state, "cycle_count", 0) if state else 0,
                    "error_count": getattr(state, "error_count", 0) if state else 0,
                    "last_error": getattr(state, "last_error", None) if state else None,
                    "last_poll_at": (
                        getattr(state, "last_poll_at", None).isoformat()  # type: ignore[union-attr]
                        if state and isinstance(getattr(state, "last_poll_at", None), datetime)
                        else None
                    ),
                })

            return JSONResponse({
                "symphonies": symphonies,
                "config_version": config_version,
            }, headers=_version_headers(config_path))


def _register_get_symphony(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon
        config_path = ctx.config_path

        @app.get("/api/symphonies/{name}")
        async def get_symphony(name: str) -> JSONResponse:
            """Get detailed state for a specific symphony (Task 9)."""
            symphony_configs = daemon.state.get("symphony_configs") or {}
            symphony_states = daemon.state.get("symphony_states") or {}

            if name not in symphony_configs:
                return JSONResponse(
                    {"error": f"Symphony {name!r} not found"},
                    status_code=404,
                )

            cfg = symphony_configs[name]
            state = symphony_states.get(name)

            env_cache = daemon.state.get("env_cache") or {}
            ec = env_cache.get(name)
            env_cache_payload: dict[str, Any] | None = None
            if ec is not None:
                env_cache_payload = {
                    "sanitised_name": getattr(ec, "sanitised_name", None),
                    "cache_dir": str(getattr(ec, "cache_dir", "")) or None,
                    "readme_sha": getattr(ec, "readme_sha", None),
                    "bootstrap_in_flight": bool(getattr(ec, "bootstrap_in_flight", False)),
                    "cache_dir_ready": bool(getattr(ec, "cache_dir_ready", False)),
                    "last_bootstrap_at": (
                        ec.last_bootstrap_at.isoformat()
                        if isinstance(getattr(ec, "last_bootstrap_at", None), datetime)
                        else None
                    ),
                    "last_bootstrap_succeeded": getattr(ec, "last_bootstrap_succeeded", None),
                    "last_bootstrap_error": getattr(ec, "last_bootstrap_error", None),
                    "environment_assessment": getattr(ec, "environment_assessment", None),
                    "environment_verification": getattr(ec, "environment_verification", None),
                    # 063 T026d: service-inference summary
                    "last_inference_at": (
                        ec.last_inference_at.isoformat()
                        if isinstance(getattr(ec, "last_inference_at", None), datetime)
                        else None
                    ),
                    "last_inference_skipped_reason": getattr(ec, "last_inference_skipped_reason", None),
                    "last_inference_agent_version": getattr(ec, "last_inference_agent_version", None),
                    "last_inference_attempts": getattr(ec, "last_inference_attempts", None),
                    "last_inference_succeeded": getattr(ec, "last_inference_succeeded", None),
                    "last_inference_services": list(getattr(ec, "last_inference_services", []) or []),
                }

            return JSONResponse({
                "name": name,
                "github_project_number": getattr(cfg, "github_project_number", None),
                "enabled": getattr(cfg, "enabled", True),
                "overrides": _mask_secret_overrides(getattr(cfg, "overrides", None) or {}),
                "secret_overrides": _secret_override_fields(daemon, getattr(cfg, "overrides", None) or {}),
                "personas": getattr(cfg, "personas", None) or {},
                "env_spec_files": getattr(cfg, "env_spec_files", None) or ["README.md"],
                "env_bootstrap_performer_id": getattr(cfg, "env_bootstrap_performer_id", None),
                "env_cache": env_cache_payload,
                "state": {
                    "cycle_count": getattr(state, "cycle_count", 0) if state else 0,
                    "error_count": getattr(state, "error_count", 0) if state else 0,
                    "last_error": getattr(state, "last_error", None) if state else None,
                    "last_poll_at": (
                        getattr(state, "last_poll_at", None).isoformat()  # type: ignore[union-attr]
                        if state and isinstance(getattr(state, "last_poll_at", None), datetime)
                        else None
                    ),
                    "active_card": getattr(state, "active_card", None) if state else None,
                    "board_snapshot": getattr(state, "board_snapshot", None) if state else None,
                } if state is not None else None,
            }, headers=_version_headers(config_path))


def _symphony_write_refusal(proposed: dict[str, Any], config_path: Path | None) -> JSONResponse | None:
    """225 round 2: refuse a symphony write the daemon could not start with, before any state changes.

    The proposed symphony list is checked as the daemon would load it, with the same candidate
    validation and start-time checks as every other config write. In-memory-only mode (no file)
    has nothing to check against and is allowed.
    """
    if config_path is None or not config_path.is_file():
        return None
    loaded = yaml.safe_load(config_path.read_text())
    if not isinstance(loaded, dict):
        return None
    candidate = {**loaded, "symphonies": symphony_documents(proposed, loaded)}
    errors = _validate_candidate(candidate, original=loaded)
    if not errors:
        return None
    detail = "; ".join(err.message for err in errors)
    return JSONResponse({"error": f"Validation failed: {detail}"}, status_code=422)


def _effective_config_refusal(new_cfg: Any, daemon: Any, name: str) -> JSONResponse | None:
    """Cross-check a symphony against the global defaults, or return the refusal."""
    coordinare_cfg = daemon.state.get("coordinare_config")
    if coordinare_cfg is not None:
        try:
            new_cfg.effective_config(coordinare_cfg.global_config)
        except Exception as exc:
            _log_validation_failure("symphony_effective_config_failed", name, exc)
            if isinstance(exc, PydanticValidationError):
                msg = "; ".join(
                    f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
                )
            elif isinstance(exc, UnknownPersonaRoleError):
                msg = exc.public_message
            else:
                msg = str(exc)
            return JSONResponse({"error": msg}, status_code=400)
    return None


def _parse_symphony_project_number(
    body: dict[str, Any],
) -> tuple[Any, JSONResponse | None]:
    """Validate the github_project_number body field, or return the refusal."""
    github_project_number = body.get("github_project_number")
    if not github_project_number:
        return None, JSONResponse({"error": "github_project_number is required"}, status_code=400)
    if isinstance(github_project_number, bool) or not isinstance(github_project_number, int):
        return None, JSONResponse({"error": "github_project_number must be an integer"}, status_code=400)
    return github_project_number, None


def _construct_new_symphony(
    daemon: Any, body: dict[str, Any], name: str, github_project_number: int,
) -> tuple[Any, JSONResponse | None]:
    """Build and cross-check the new SymphonyConfig, or return the refusal."""
    from coordinare.config import SymphonyConfig

    try:
        new_cfg = SymphonyConfig(
            name=name,
            github_project_number=int(github_project_number),
            overrides=body.get("overrides") or None,
            personas=body.get("personas") or None,
            env_spec_files=body.get("env_spec_files") or ["README.md"],
        )
    except Exception as exc:
        _log_validation_failure("symphony_create_validation_failed", name, exc)
        return None, JSONResponse({"error": "Invalid symphony configuration"}, status_code=400)

    refusal = _effective_config_refusal(new_cfg, daemon, name)
    if refusal is not None:
        return None, refusal

    return new_cfg, None


def _register_create_symphony(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon
        config_path = ctx.config_path

        @app.post("/api/symphonies")
        async def create_symphony(request: Request) -> JSONResponse:
            """Create a new symphony (Task 9)."""

            # Best-effort guard: rejects requests when a cycle is actively running.
            # A race between this check and the daemon starting a new cycle is possible
            # but harmless — the next cycle will load the persisted config anyway.
            if daemon._cycle_active:
                return JSONResponse(
                        {"error": "A cycle is in progress — please try again shortly", "status": "cycle_in_progress"},
                        status_code=409,
                    )

            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

            if not isinstance(body, dict):
                return JSONResponse({"error": "Request body must be a JSON object"}, status_code=400)

            raw_name = body.get("name", "")
            if not isinstance(raw_name, str):
                return JSONResponse({"error": "name must be a string"}, status_code=400)
            name = raw_name.strip()
            if not name:
                return JSONResponse({"error": "name is required"}, status_code=400)

            github_project_number, refusal = _parse_symphony_project_number(body)
            if refusal is not None:
                return refusal

            symphony_configs = dict(daemon.state.get("symphony_configs") or {})

            if name in symphony_configs:
                return JSONResponse({"error": f"Symphony {name!r} already exists"}, status_code=409)

            new_cfg, refusal = _construct_new_symphony(daemon, body, name, github_project_number)
            if refusal is not None:
                return refusal

            # 158 (#241): after validation, so "already exists" and a malformed body stay
            # 400/409 rather than becoming a demand for a version to do something that was
            # never going to happen -- but BEFORE the mutation below, because this handler
            # writes daemon.state before it writes the file. A guard placed beside the
            # write refuses having already added the symphony to memory and bumped
            # config_version, which is a worse outcome than the overwrite it prevented.
            # That was the bug in delete_symphony; it was in this handler and in
            # update_symphony too, and the tests missed it because they compared file
            # bytes and only checked memory for delete.
            refusal = _version_refusal(config_path, request.headers.get("If-Match"))
            if refusal is not None:
                return refusal

            refusal = _symphony_write_refusal({**symphony_configs, name: new_cfg}, config_path)
            if refusal is not None:
                return refusal

            saved_version = daemon.state.get("config_version", 0)
            symphony_configs[name] = new_cfg
            daemon.state["symphony_configs"] = symphony_configs
            daemon.state["config_version"] = saved_version + 1
            try:
                _persist_symphony_configs(symphony_configs, config_path)
            except ValueError:
                _log.debug("symphony_create_persist_skipped_no_config_file", name=name)
            except Exception as exc:
                # Roll back in-memory state so the API contract stays atomic.
                del symphony_configs[name]
                daemon.state["symphony_configs"] = symphony_configs
                daemon.state["config_version"] = saved_version
                _log_validation_failure("symphony_persist_failed", name, exc)
                return JSONResponse({"error": "Failed to persist symphony configuration"}, status_code=500)

            if hasattr(daemon, "_config_reload_trigger"):
                daemon._config_reload_trigger.set()
            if hasattr(daemon, "_webhook_trigger"):
                daemon._webhook_trigger.set()

            return JSONResponse({
                "name": name,
                "github_project_number": new_cfg.github_project_number,
                "enabled": new_cfg.enabled,
                "overrides": new_cfg.overrides or {},
                "personas": new_cfg.personas or {},
                "env_spec_files": new_cfg.env_spec_files,
            }, status_code=201, headers=_version_headers(config_path))


def _register_validate_symphony(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon

        @app.post("/api/symphonies/{name}/validate")
        async def validate_symphony(name: str, request: Request) -> JSONResponse:
            """Validate a symphony's configuration (Task 9). Accepts optional body with proposed overrides/personas for dry-run validation."""
            from coordinare.config import SymphonyConfig

            symphony_configs = daemon.state.get("symphony_configs") or {}

            if name not in symphony_configs:
                return JSONResponse(
                    {"error": f"Symphony {name!r} not found"},
                    status_code=404,
                )

            cfg = symphony_configs[name]
            coordinare_cfg = daemon.state.get("coordinare_config")

            if not coordinare_cfg:
                return JSONResponse(
                    {"error": "Coordinare configuration not available"},
                    status_code=500,
                )

            # Accept optional body with proposed overrides/personas for dry-run validation
            proposed_overrides = getattr(cfg, "overrides", None)
            proposed_personas = getattr(cfg, "personas", None)
            raw_body = await request.body()
            if raw_body:
                try:
                    body = await request.json()
                except Exception:
                    return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
                if not isinstance(body, dict):
                    return JSONResponse(
                        {"error": "Request body must be a JSON object"}, status_code=400,
                    )
                proposed_overrides = body.get("overrides", proposed_overrides)
                proposed_personas = body.get("personas", proposed_personas)

            try:
                candidate = SymphonyConfig(
                    name=name,
                    github_project_number=getattr(cfg, "github_project_number", None),  # type: ignore[arg-type]
                    overrides=proposed_overrides,
                    personas=proposed_personas,
                )
                effective = candidate.effective_config(coordinare_cfg.global_config)
                return JSONResponse({
                    "valid": True,
                    "symphony": name,
                    "effective_config": {
                        "github_org": effective.github_org,
                        "github_project_number": effective.github_project_number,
                        "project_name": effective.project_name,
                    },
                })
            except PydanticValidationError as exc:
                return JSONResponse(
                    {"valid": False, "errors": exc.errors(include_context=False, include_input=False)},
                    status_code=400,
                )
            except ValueError as exc:
                return JSONResponse(
                    {"valid": False, "errors": [{"msg": exc.public_message if isinstance(exc, UnknownPersonaRoleError) else str(exc)}]},
                    status_code=400,
                )
            except Exception as exc:
                _log_validation_failure("symphony_validate_unexpected_error", name, exc)
                return JSONResponse({"error": "Internal validation error"}, status_code=500)


def _register_update_symphony(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon
        config_path = ctx.config_path

        @app.put("/api/symphonies/{name}")
        async def update_symphony(name: str, request: Request) -> JSONResponse:
            """Update a symphony's overrides or personas (Task 9)."""
            from coordinare.config import SymphonyConfig

            if daemon._cycle_active:
                return JSONResponse(
                        {"error": "A cycle is in progress — please try again shortly", "status": "cycle_in_progress"},
                        status_code=409,
                    )

            # 158 (#241): a copy, as create_symphony already took. This handler mutates
            # the dict before it persists, and the live daemon.state one would make any
            # statement that lands above the version guard corrupt shared state the
            # instant it runs, with nothing to roll back from. The guard is above every
            # mutation today and a test asserts that; this makes the ordering a bug
            # rather than a catastrophe if it ever stops holding.
            symphony_configs = dict(daemon.state.get("symphony_configs") or {})

            if name not in symphony_configs:
                return JSONResponse({"error": f"Symphony {name!r} not found"}, status_code=404)

            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

            if not isinstance(body, dict):
                return JSONResponse({"error": "Request body must be a JSON object"}, status_code=400)

            cfg = symphony_configs[name]
            enabled = body.get("enabled", getattr(cfg, "enabled", True))
            stored_overrides = getattr(cfg, "overrides", None) or {}
            overrides, refusal = _restore_masked_secrets(body.get("overrides", stored_overrides), stored_overrides)
            if refusal is not None:
                return refusal
            try:
                updated = SymphonyConfig(
                    name=name,
                    github_project_number=getattr(cfg, "github_project_number", None),  # type: ignore[arg-type]
                    enabled=enabled,
                    overrides=overrides,
                    personas=body.get("personas", getattr(cfg, "personas", None)),
                    env_spec_files=body.get("env_spec_files", getattr(cfg, "env_spec_files", ["README.md"])),
                )
            except Exception as exc:
                _log_validation_failure("symphony_config_validation_failed", name, exc)
                return JSONResponse({"error": "Invalid symphony configuration"}, status_code=400)

            refusal = _effective_config_refusal(updated, daemon, name)
            if refusal is not None:
                return refusal

            # 158 (#241): before the mutation, for the reason spelt out in create_symphony.
            refusal = _version_refusal(config_path, request.headers.get("If-Match"))
            if refusal is not None:
                return refusal

            refusal = _symphony_write_refusal({**symphony_configs, name: updated}, config_path)
            if refusal is not None:
                return refusal

            saved_version = daemon.state.get("config_version", 0)
            previous_cfg = symphony_configs.get(name)
            symphony_configs[name] = updated
            daemon.state["symphony_configs"] = symphony_configs
            daemon.state["config_version"] = saved_version + 1
            try:
                _persist_symphony_configs(symphony_configs, config_path)
            except ValueError:
                # config_path is None or file doesn't exist — in-memory-only mode, not an error.
                _log.debug("symphony_update_persist_skipped_no_config_file", name=name)
            except Exception as exc:
                # Actual I/O error — roll back so the in-memory state stays consistent.
                if previous_cfg is not None:
                    symphony_configs[name] = previous_cfg
                else:
                    del symphony_configs[name]
                daemon.state["symphony_configs"] = symphony_configs
                daemon.state["config_version"] = saved_version
                _log_validation_failure("symphony_persist_failed", name, exc)
                return JSONResponse({"error": "Failed to persist symphony configuration"}, status_code=500)

            if hasattr(daemon, "_config_reload_trigger"):
                daemon._config_reload_trigger.set()
            if hasattr(daemon, "_webhook_trigger"):
                daemon._webhook_trigger.set()

            return JSONResponse({
                "name": name,
                "enabled": getattr(updated, "enabled", True),
                "overrides": _mask_secret_overrides(getattr(updated, "overrides", None) or {}),
                "personas": getattr(updated, "personas", None) or {},
                "env_spec_files": getattr(updated, "env_spec_files", ["README.md"]),
            }, headers=_version_headers(config_path))


def _register_delete_symphony(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon
        config_path = ctx.config_path

        @app.delete("/api/symphonies/{name}")
        async def delete_symphony(name: str, request: Request) -> JSONResponse:
            """Remove a symphony (Task 9). Returns 409 if it would remove the last symphony."""
            if daemon._cycle_active:
                return JSONResponse(
                        {"error": "A cycle is in progress — please try again shortly", "status": "cycle_in_progress"},
                        status_code=409,
                    )

            # 158 (#241): a copy, as create_symphony already took. This handler mutates
            # the dict before it persists, and the live daemon.state one would make any
            # statement that lands above the version guard corrupt shared state the
            # instant it runs, with nothing to roll back from. The guard is above every
            # mutation today and a test asserts that; this makes the ordering a bug
            # rather than a catastrophe if it ever stops holding.
            symphony_configs = dict(daemon.state.get("symphony_configs") or {})

            if name not in symphony_configs:
                return JSONResponse({"error": f"Symphony {name!r} not found"}, status_code=404)

            if len(symphony_configs) <= 1:
                return JSONResponse(
                    {"error": "Cannot delete the last symphony"},
                    status_code=409,
                )

            symphony_states = daemon.state.get("symphony_states") or {}
            sym_state = symphony_states.get(name)
            if sym_state is not None and getattr(sym_state, "active_sessions", None):
                return JSONResponse(
                    {
                        "error": "Symphony has active sessions; wait for them to complete before deleting",
                        "active_sessions": list(sym_state.active_sessions.keys()),
                    },
                    status_code=409,
                )

            # 158 (#241): the last thing before any mutation. All three symphony handlers
            # write daemon.state before they persist, so a guard placed by the write
            # refuses having already made the change it is refusing. This was found here
            # first, and the claim that the other four "sit beside the write" was then
            # left standing for two commits while create_symphony and update_symphony had
            # the same bug -- because the in-memory test was written for this handler
            # only, and the other four were checked by comparing file bytes.
            # TestARefusalTouchesNoStateAtAll now covers all five, and asserts the
            # ordering structurally so it cannot drift back.
            refusal = _version_refusal(config_path, request.headers.get("If-Match"))
            if refusal is not None:
                return refusal

            saved_configs = dict(symphony_configs)
            saved_states = dict(symphony_states)
            refusal = _symphony_write_refusal(
                {k: v for k, v in symphony_configs.items() if k != name}, config_path,
            )
            if refusal is not None:
                return refusal

            saved_version = daemon.state.get("config_version", 0)

            del symphony_configs[name]
            daemon.state["symphony_configs"] = symphony_configs
            daemon.state["config_version"] = saved_version + 1

            symphony_states.pop(name, None)
            daemon.state["symphony_states"] = symphony_states

            try:
                _persist_symphony_configs(symphony_configs, config_path)
            except ValueError:
                _log.debug("symphony_persist_skipped_no_config_file", name=name)
            except Exception as exc:
                # Roll back in-memory state so the config and disk stay in sync.
                daemon.state["symphony_configs"] = saved_configs
                daemon.state["symphony_states"] = saved_states
                daemon.state["config_version"] = saved_version
                _log_validation_failure("symphony_persist_failed", name, exc)
                return JSONResponse({"error": "Failed to persist config; delete rolled back"}, status_code=500)

            if hasattr(daemon, "_config_reload_trigger"):
                daemon._config_reload_trigger.set()
            if hasattr(daemon, "_webhook_trigger"):
                daemon._webhook_trigger.set()

            return JSONResponse({"deleted": name}, headers=_version_headers(config_path))


def _register_trigger_env_bootstrap(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon

        @app.post("/api/symphonies/{name}/env-bootstrap")
        async def trigger_env_bootstrap(name: str, request: Request) -> JSONResponse:
            """Force an env_bootstrap dispatch for a symphony.

            Resets the retry budget and breaker (270), clears readme_sha so the next cycle
            dispatches, and records who triggered it. Paused symphonies get 409 and nothing
            is queued. Returns 202 on accept; 409 if paused or in flight.
            """
            symphony_configs = daemon.state.get("symphony_configs") or {}
            if name not in symphony_configs:
                return JSONResponse(
                    {"error": f"Symphony {name!r} not found"}, status_code=404,
                )

            sym_cfg = symphony_configs[name]
            if getattr(sym_cfg, "env_bootstrap_performer_id", None) is None:
                return JSONResponse(
                    {
                        "error": (
                            f"Symphony {name!r} has no env_bootstrap_performer_id "
                            "configured"
                        ),
                    },
                    status_code=400,
                )

            if not getattr(sym_cfg, "enabled", True):
                msg = f"Symphony {name!r} is paused (enabled: false); enable it before forcing a bootstrap"
                return JSONResponse({"error": msg, "status": "symphony_paused"}, status_code=409)

            env_cache_svc = daemon.state.get("env_cache_service")
            if env_cache_svc is None:
                return JSONResponse(
                    {"error": "Env cache service not available"}, status_code=503,
                )

            performer_id = sym_cfg.env_bootstrap_performer_id
            performer_svcs = daemon.state.get("performer_services_by_id") or {}
            if performer_id not in performer_svcs:
                return JSONResponse(
                    {
                        "error": (
                            f"Bootstrap performer {performer_id!r} is not "
                            "registered with the daemon; check that it is "
                            "defined in config.yaml and that coordinare loaded "
                            "it at startup."
                        ),
                        "performer_id": performer_id,
                    },
                    status_code=503,
                )

            env_cache = daemon.state.get("env_cache") or {}
            cache_state = env_cache.get(name)
            if cache_state is None:
                return JSONResponse(
                    {
                        "error": (
                            f"Symphony {name!r} env cache state has not been "
                            "initialised yet; wait one cycle and retry"
                        ),
                    },
                    status_code=503,
                )

            if getattr(cache_state, "bootstrap_in_flight", False):
                return JSONResponse(
                    {
                        "error": "A bootstrap is already in flight for this symphony",
                        "status": "bootstrap_in_flight",
                    },
                    status_code=409,
                )

            try:
                next_reset = await request_env_bootstrap_reset(
                    daemon.state, name, cache_state, _session_actor(request), ctx.store.activity_log,
                )
            except EnvironmentDiscoveryError as exc:
                return JSONResponse({"error": str(exc), "status": exc.code}, status_code=409)
            if next_reset is not None:
                allowed_at = next_reset.isoformat()
                return JSONResponse({
                    "error": f"Manual bootstrap reset cooldown; next reset is allowed at {allowed_at}",
                    "status": "bootstrap_reset_cooldown", "next_reset_at": allowed_at,
                }, status_code=429)
            # Force the next cycle to detect a SHA mismatch and re-dispatch.
            cache_state.readme_sha = None
            if hasattr(daemon, "_webhook_trigger"):
                daemon._webhook_trigger.set()

            return JSONResponse(
                {
                    "status": "accepted",
                    "symphony": name,
                    "performer_id": performer_id,
                },
                status_code=202,
            )


def _register_trigger_wiki_init(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon

        @app.post("/api/symphonies/{name}/wiki-init")
        async def trigger_wiki_init(name: str) -> JSONResponse:
            """Manually seed a symphony's living docs/wiki (spec 124 US2).

            Records a wiki-init request that the daemon's next cycle dispatches: our
            documenter runs in ``init`` mode against the repo, opens a seed PR, and
            WikiInitService auto-merges it on CI-green + trusted-bot approval. Operator
            -initiated (no auto-gate), so it never holds other dispatch. 202 on accept;
            404 unknown symphony; 409 if a wiki-init is already in flight; 503 if the
            env-cache state isn't ready yet.
            """
            symphony_configs = daemon.state.get("symphony_configs") or {}
            if name not in symphony_configs:
                return JSONResponse({"error": f"Symphony {name!r} not found"}, status_code=404)

            cache_state = (daemon.state.get("env_cache") or {}).get(name)
            if cache_state is None:
                return JSONResponse(
                    {"error": (
                        f"Symphony {name!r} env-cache state has not been initialised "
                        "yet — wait one cycle and retry"
                    )},
                    status_code=503,
                )
            if getattr(cache_state, "wiki_in_flight", False):
                return JSONResponse(
                    {"error": "A wiki-init is already in flight for this symphony",
                     "status": "wiki_in_flight"},
                    status_code=409,
                )

            # Record the manual request; the daemon cycle drains it (mirrors the
            # env-bootstrap flag-then-poke pattern). ``_wiki_init_requests`` is always
            # created in the daemon's __init__.
            daemon._wiki_init_requests.add(name)
            if hasattr(daemon, "_webhook_trigger"):
                daemon._webhook_trigger.set()

            return JSONResponse(
                {"status": "accepted", "symphony": name}, status_code=202,
            )


def register(app: FastAPI, ctx: DashboardContext) -> None:
    _register_get_symphonies(app, ctx)
    _register_get_symphony(app, ctx)
    _register_create_symphony(app, ctx)
    _register_validate_symphony(app, ctx)
    _register_update_symphony(app, ctx)
    _register_delete_symphony(app, ctx)
    _register_trigger_env_bootstrap(app, ctx)
    _register_trigger_wiki_init(app, ctx)
