"""Config read/write/catalog routes (436)."""
from __future__ import annotations

import asyncio  # noqa: F401
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import (  # noqa: TC002 — FastAPI resolves handler signatures at registration; runtime import required
    FastAPI,
    Request,
)
from fastapi.responses import JSONResponse

from coordinare.dashboard.routers.config_write import (
    _atomic_write_yaml,
    _global_cfg_editable,
    _global_unguarded_refusal,
    _guard_global_hash,
    _validate_global_config,
)

if TYPE_CHECKING:
    from pathlib import Path

    from coordinare.dashboard.context import DashboardContext

_log = structlog.get_logger(__name__)


def _build_config_snapshot_inputs(daemon: Any, config_path: Path | None) -> tuple[Any, dict[str, str | None], dict[str, Any], bool, bool]:
    """Gather the shared inputs for ``build_snapshot`` / ``build_section`` (T016/T017).

    Returns ``(coordinare_cfg, content_hashes, raw_values, routing_avail,
    routing_mounted)``. ``routing_mounted`` is true whenever a performer
    endpoint mounts a routing path (even if its host file is missing), so the
    empty-state banner can distinguish "unmounted" from "mounted-but-broken".
    Display values (``raw_values``) come from the raw on-disk YAML so ``${VAR}``
    literals are preserved verbatim (research D3); the validated config object
    can never hold an unresolved placeholder for the pat ``github_token``.
    """
    from coordinare import routing_config_service
    from coordinare.services.config_write_service import compute_content_hash

    coordinare_cfg = daemon.state.get("coordinare_config")

    content_hashes: dict[str, str | None] = {}
    raw_values: dict[str, Any] = {}
    if config_path is not None and config_path.is_file():
        import yaml as _yaml

        # Tolerant read: an out-of-band edit / mount glitch can leave the file
        # temporarily unreadable or YAML-malformed. The descriptor layer can
        # still render from the in-memory validated config, so degrade only the
        # raw "${VAR}" display values rather than 500 the whole display fetch.
        try:
            content_hashes["config_yaml"] = compute_content_hash(config_path)
            loaded = _yaml.safe_load(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, _yaml.YAMLError):
            content_hashes.setdefault("config_yaml", None)
            loaded = None
        if isinstance(loaded, dict):
            for fname, fval in loaded.items():
                if not isinstance(fval, (dict, list)):
                    raw_values[f"global.{fname}"] = fval

    routing_avail = False
    routing_mounted = False
    if coordinare_cfg is not None:
        endpoints = coordinare_cfg.global_config.performer_endpoints
        routing_avail = routing_config_service.routing_available(endpoints)
        location = routing_config_service.locate_routing_file(endpoints)
        routing_mounted = location is not None
        if location is not None and location.host_path.is_file():
            content_hashes["routing_yaml"] = compute_content_hash(location.host_path)
        else:
            content_hashes["routing_yaml"] = None

    return coordinare_cfg, content_hashes, raw_values, routing_avail, routing_mounted

def _save_status_code(result: Any) -> int:
    """Map a ``SaveResult`` to an HTTP status (T032).

    ok → 200; otherwise the first error's code decides:
    conflict/referenced → 409, validation → 422, forbidden → 403.
    Messages are already secret-free and stack-trace-free at the service layer.
    """
    if result.ok:
        return 200
    code = result.errors[0].code if result.errors else "validation"
    return {
        "conflict": 409,
        "referenced": 409,
        "validation": 422,
        "forbidden": 403,
    }.get(code, 422)

def _trigger_reload(daemon: Any) -> None:
    """Fire the daemon's hot-reload trigger (and wake the loop). May raise."""
    daemon._config_reload_trigger.set()
    if hasattr(daemon, "_webhook_trigger"):
        daemon._webhook_trigger.set()

def _apply_reload_or_stage(daemon: Any, result: Any, what: str) -> None:
    """Fire the hot-reload trigger for a successful write, enforcing FR-015.

    On a successful ``hot_reloaded`` write the daemon reload trigger is fired.
    If the trigger raises after the atomic write, the change stays on disk but
    the live config is NOT swapped — downgrade the result to ``staged_restart``
    with an operator-readable, secret-free advisory (mirroring
    ``put_config_section``) rather than falsely reporting the change as live.
    """
    if not (result.ok and result.applied == "hot_reloaded"):
        return
    if not hasattr(daemon, "_config_reload_trigger"):
        return
    try:
        _trigger_reload(daemon)
    except Exception:
        _log.warning("config_reload_failed", what=what)
        result.applied = "staged_restart"
        result.message = (
            "Saved to disk, but the live reload failed; restart the "
            "coordinare to apply this change."
        )

def _routing_location(daemon: Any) -> Any:
    """Resolve the host-side routing-table location from the live config, or None.

    Returns the :class:`RoutingLocation` whenever a performer endpoint mounts a
    routing path — *even when the host-side file is missing or not a regular
    file*. The writability decision (and its accurate operator-facing message)
    belongs to ``routing_config_service._readonly_guard()``: collapsing a
    mounted-but-missing location to ``None`` here would lose the actionable
    "endpoint X mounts a path that isn't a file" diagnostic and emit the
    generic "no endpoint mounts a routing table" message instead. ``None`` is
    returned only when no endpoint mounts a routing table at all.
    """
    from coordinare import routing_config_service

    coordinare_cfg = daemon.state.get("coordinare_config")
    if coordinare_cfg is None:
        return None
    endpoints = coordinare_cfg.global_config.performer_endpoints
    return routing_config_service.locate_routing_file(endpoints)

def _register_get_config_all(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon
        config_path = ctx.config_path
        daemon = ctx.daemon

        @app.get("/api/config/all")
        async def get_config_all() -> JSONResponse:
            """Return the full editable config surface as a ``ConfigSnapshot`` (T016).

            Descriptors + content_hashes + config_version + routing_available. Secrets
            are masked and ``${VAR}`` placeholders preserved by the descriptor layer.
            """
            from coordinare import config_descriptors as cd

            coordinare_cfg, content_hashes, raw_values, routing_avail, routing_mounted = (
                _build_config_snapshot_inputs(daemon, config_path)
            )
            if coordinare_cfg is None:
                return JSONResponse({"error": "Config not available"}, status_code=500)

            config_version = daemon.state.get("config_version", 0)
            snapshot = cd.build_snapshot(
                coordinare_cfg,
                config_version=config_version,
                routing_available=routing_avail,
                routing_mounted=routing_mounted,
                content_hashes=content_hashes,
                raw_values=raw_values,
            )
            return JSONResponse(snapshot.model_dump(mode="json"))


def _register_get_config_section(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon
        config_path = ctx.config_path

        @app.get("/api/config/section/{section_id}")
        async def get_config_section(section_id: str) -> JSONResponse:
            """Return a single config section by id; 404 on unknown id (T017)."""
            from fastapi import HTTPException

            from coordinare import config_descriptors as cd

            coordinare_cfg, content_hashes, raw_values, routing_avail, routing_mounted = (
                _build_config_snapshot_inputs(daemon, config_path)
            )
            if coordinare_cfg is None:
                return JSONResponse({"error": "Config not available"}, status_code=500)

            try:
                section = cd.build_section(
                    coordinare_cfg,
                    section_id,
                    routing_available=routing_avail,
                    routing_mounted=routing_mounted,
                    content_hashes=content_hashes,
                    raw_values=raw_values,
                )
            except KeyError as err:
                raise HTTPException(
                    status_code=404, detail=f"Unknown section: {section_id}",
                ) from err
            return JSONResponse(section.model_dump(mode="json"))


def _register_get_effective_config(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon

        @app.get("/api/config/effective")
        async def get_effective_config(symphony: str | None = None) -> JSONResponse:
            """Get effective configuration, optionally scoped to a symphony (Task 9)."""
            coordinare_cfg = daemon.state.get("coordinare_config")
            cfg = coordinare_cfg.global_config if coordinare_cfg else daemon.state.get("config")

            if not cfg:
                return JSONResponse(
                    {"error": "Config not available"},
                    status_code=500,
                )

            if symphony:
                symphony_configs = daemon.state.get("symphony_configs") or {}
                if symphony not in symphony_configs:
                    return JSONResponse(
                        {"error": f"Symphony {symphony!r} not found"},
                        status_code=404,
                    )
                sym_cfg = symphony_configs[symphony]
                try:
                    effective = sym_cfg.effective_config(cfg)
                    return JSONResponse({
                        "github_org": effective.github_org,
                        "github_project_number": effective.github_project_number,
                        "project_name": effective.project_name,
                        "symphony": symphony,
                        "mode": "symphony",
                    })
                except Exception:
                    _log.error("effective_config_resolution_failed", symphony=symphony, exc_info=True)
                    return JSONResponse(
                        {"error": "Failed to compute effective config for this symphony"},
                        status_code=500,
                    )

            return JSONResponse({
                "github_org": cfg.github_org,
                "github_project_number": cfg.github_project_number,
                "project_name": cfg.project_name,
                "mode": daemon.state.get("config_mode", "legacy"),
            })


def _register_reload_config(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon

        @app.post("/api/config/reload")
        async def reload_config() -> JSONResponse:
            """Trigger a hot-reload of the configuration (Task 10)."""
            if not hasattr(daemon, "_config_reload_trigger"):
                return JSONResponse(
                    {"error": "Config reload not supported"},
                    status_code=501,
                )

            daemon._config_reload_trigger.set()
            # Wake the daemon loop in case it's blocked waiting on _webhook_trigger
            # (e.g. poll_interval_seconds=0 / webhook-only mode)
            if hasattr(daemon, "_webhook_trigger"):
                daemon._webhook_trigger.set()
            return JSONResponse({
                "status": "reload_triggered",
                "message": "Configuration reload in progress",
            }, status_code=202)


def _register_get_global_config(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon
        config_path = ctx.config_path

        @app.get("/api/config/global")
        async def get_global_config() -> JSONResponse:
            """Return the editable global config fields."""
            coordinare_cfg = daemon.state.get("coordinare_config")
            cfg = coordinare_cfg.global_config if coordinare_cfg else daemon.state.get("config")
            if not cfg:
                return JSONResponse({"error": "Config not available"}, status_code=500)
            def _serialize(v: object) -> object:
                from pathlib import Path as _Path
                return str(v) if isinstance(v, _Path) else v

            body = {k: _serialize(getattr(cfg, k, None)) for k in _global_cfg_editable}
            headers: dict[str, str] = {}
            # 156 (#237): the page needs the file's content hash to save safely, and had
            # no way to get one -- this endpoint returns values only, and the hash lives
            # in the far heavier /api/config/all. It cannot go in the body either: an
            # existing test pins this response's exact key set, correctly, because that
            # is a values contract. An ETag is what the header is for, and adds nothing
            # to the body.
            if config_path is not None and config_path.is_file():
                from coordinare.services.config_write_service import compute_content_hash

                # Quoted, per RFC 7232 §2.3: an entity-tag is a DQUOTE-enclosed opaque
                # tag. A bare sha256:... happens to work for our own string comparison
                # and is still a malformed header, which is the kind of thing that works
                # until something between us and the browser starts caring.
                headers["ETag"] = f'"{compute_content_hash(config_path)}"'
            return JSONResponse(body, headers=headers)


def _register_update_global_config(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon
        config_path = ctx.config_path

        @app.put("/api/config/global")
        async def update_global_config(request: Request) -> JSONResponse:
            """Persist editable global config fields to config.yaml and trigger reload."""
            import yaml

            from coordinare.services.config_write_service import compute_content_hash

            if config_path is None or not config_path.is_file():
                return JSONResponse(
                    {"error": "Config file not available; changes cannot be persisted"},
                    status_code=503,
                )

            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

            if not isinstance(body, dict):
                return JSONResponse({"error": "Request body must be a JSON object"}, status_code=400)

            # 155 (#202): optional optimistic-concurrency guard. The catalog and routing
            # endpoints have carried one since spec 081; this one never did, so two
            # dashboards open on the same config could silently overwrite each other
            # (#237). Optional rather than required so no existing caller changes
            # behaviour -- the config assistant's Apply always sends one, so every
            # assistant-driven write is guarded even while the older UI is not.
            expected_hash = body.pop("expected_hash", None)
            if expected_hash is None:
                # 158: If-Match is the mechanism the other writes use, so accept it here
                # too and document one thing. The body field wins when both are sent,
                # because callers written against 157 already rely on it.
                expected_hash = request.headers.get("If-Match")
            if expected_hash is None:
                return _global_unguarded_refusal()
            if expected_hash is not None:
                refusal = _guard_global_hash(config_path, expected_hash)
                if refusal is not None:
                    return refusal

            unknown = set(body) - set(_global_cfg_editable)
            if unknown:
                return JSONResponse({"error": f"Unknown fields: {sorted(unknown)}"}, status_code=400)

            loaded = yaml.safe_load(config_path.read_text())
            if not isinstance(loaded, dict):
                return JSONResponse({"error": "Config file is not a YAML mapping"}, status_code=500)

            for k, v in body.items():
                if v is None:
                    loaded.pop(k, None)
                else:
                    loaded[k] = v

            validation_failure = _validate_global_config(loaded)
            if validation_failure is not None:
                return validation_failure

            _atomic_write_yaml(config_path, loaded)

            if hasattr(daemon, "_config_reload_trigger"):
                daemon._config_reload_trigger.set()
                if hasattr(daemon, "_webhook_trigger"):
                    daemon._webhook_trigger.set()

            # 156: hand back the new version, the way the catalog endpoints already do
            # (`new_hash`, consumed at the 081 page's save). Without it a page that saved
            # successfully would have no current hash, and its NEXT save would go
            # unguarded -- the guard would hold exactly once per page load, which is
            # worse than useless because it looks like it holds always.
            saved_hash = (
                compute_content_hash(config_path)
                if config_path is not None and config_path.is_file()
                else None
            )
            return JSONResponse({
                "status": "saved",
                "reload_triggered": hasattr(daemon, "_config_reload_trigger"),
                "new_hash": saved_hash,
            })


def _register_put_config_section(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon
        config_path = ctx.config_path

        @app.put("/api/config/section/{section_id}")
        async def put_config_section(section_id: str, request: Request) -> JSONResponse:
            """Validate + persist a scalar-group section edit (T027).

            On a ``hot_reloaded`` result the daemon reload trigger is fired. FR-015
            (T047): if the trigger raises after the successful atomic write, the
            change stays on disk but the live config is NOT swapped — we downgrade
            the result to ``staged_restart`` with an operator-readable, secret-free
            advisory rather than reporting a failure.
            """
            from pydantic import ValidationError

            from coordinare.services.config_write_service import SaveRequest, save_section

            if config_path is None:
                return JSONResponse(
                    {"ok": False, "errors": [{"key": None, "code": "forbidden",
                     "message": "Config file not available; changes cannot be persisted."}]},
                    status_code=403,
                )
            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"ok": False, "errors": [{"key": None,
                    "code": "validation", "message": "Invalid JSON body."}]}, status_code=422)
            if not isinstance(body, dict):
                return JSONResponse({"ok": False, "errors": [{"key": None,
                    "code": "validation", "message": "Request body must be a JSON object."}]},
                    status_code=422)

            # The URL is authoritative: this route only ever writes the named scalar
            # section in config.yaml. Any `store`/`section` in the body is ignored so a
            # mismatched payload can't redirect the write to a different target.
            try:
                save_req = SaveRequest(
                    store="config_yaml",
                    section=section_id,
                    changes=body.get("changes", {}),
                    base_hash=body.get("base_hash", ""),
                )
            except ValidationError:
                return JSONResponse({"ok": False, "errors": [{"key": None,
                    "code": "validation", "message": "Invalid request payload."}]},
                    status_code=422)
            result = save_section(save_req, config_path)

            if result.ok and result.applied == "hot_reloaded" and hasattr(daemon, "_config_reload_trigger"):
                try:
                    _trigger_reload(daemon)
                except Exception:
                    _log.warning("config_section_reload_failed", section=section_id)
                    result.applied = "staged_restart"
                    result.message = (
                        "Saved to disk, but the live reload failed; restart the "
                        "coordinare to apply this change."
                    )

            return JSONResponse(result.model_dump(mode="json"), status_code=_save_status_code(result))


def _register_get_config_catalog(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon
        config_path = ctx.config_path

        @app.get("/api/config/catalog/{catalog}")
        async def get_config_catalog(catalog: str) -> JSONResponse:
            """Return a single spec-080 catalog with referential-integrity flags (T028)."""
            from fastapi import HTTPException

            from coordinare import config_descriptors as cd
            from coordinare.services.config_write_service import (
                CATALOG_KEYS,
                compute_content_hash,
            )

            if catalog not in CATALOG_KEYS:
                raise HTTPException(status_code=404, detail=f"Unknown catalog: {catalog}")

            coordinare_cfg = daemon.state.get("coordinare_config")
            if coordinare_cfg is None:
                return JSONResponse({"error": "Config not available"}, status_code=500)

            section = next(
                (s for s in cd._build_catalog_sections(coordinare_cfg) if s.id == catalog),
                None,
            )
            if section is None:
                raise HTTPException(status_code=404, detail=f"Unknown catalog: {catalog}")

            content_hash = (
                compute_content_hash(config_path)
                if config_path is not None and config_path.is_file()
                else None
            )
            return JSONResponse({
                "id": catalog,
                "content_hash": content_hash,
                "items": [i.model_dump(mode="json") for i in section.items],
            })


def _register_post_config_catalog(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon
        config_path = ctx.config_path
        config_path = ctx.config_path

        @app.post("/api/config/catalog/{catalog}")
        async def post_config_catalog(catalog: str, request: Request) -> JSONResponse:
            """Create a new item in a spec-080 catalog (T029)."""
            from coordinare.services.config_write_service import (
                CATALOG_KEYS,
                create_catalog_item,
            )

            if catalog not in CATALOG_KEYS:
                return JSONResponse({"ok": False, "errors": [{"key": None,
                    "code": "validation", "message": f"Unknown catalog: {catalog}"}]},
                    status_code=422)
            if config_path is None:
                return JSONResponse({"ok": False, "errors": [{"key": None,
                    "code": "forbidden", "message": "Config file not available."}]},
                    status_code=403)
            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"ok": False, "errors": [{"key": None,
                    "code": "validation", "message": "Invalid JSON body."}]}, status_code=422)
            if not isinstance(body, dict):
                return JSONResponse({"ok": False, "errors": [{"key": None,
                    "code": "validation", "message": "Request body must be a JSON object."}]},
                    status_code=422)

            result = create_catalog_item(
                catalog, body.get("item", {}), body.get("base_hash", ""), config_path,
            )
            _apply_reload_or_stage(daemon, result, f"catalog POST {catalog}")
            return JSONResponse(result.model_dump(mode="json"), status_code=_save_status_code(result))


def _register_put_config_catalog_item(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon
        config_path = ctx.config_path
        config_path = ctx.config_path

        @app.put("/api/config/catalog/{catalog}/{item_id}")
        async def put_config_catalog_item(catalog: str, item_id: str, request: Request) -> JSONResponse:
            """Update an existing item in a spec-080 catalog (T030)."""
            from coordinare.services.config_write_service import (
                CATALOG_KEYS,
                update_catalog_item,
            )

            if catalog not in CATALOG_KEYS:
                return JSONResponse({"ok": False, "errors": [{"key": None,
                    "code": "validation", "message": f"Unknown catalog: {catalog}"}]},
                    status_code=422)
            if config_path is None:
                return JSONResponse({"ok": False, "errors": [{"key": None,
                    "code": "forbidden", "message": "Config file not available."}]},
                    status_code=403)
            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"ok": False, "errors": [{"key": None,
                    "code": "validation", "message": "Invalid JSON body."}]}, status_code=422)
            if not isinstance(body, dict):
                return JSONResponse({"ok": False, "errors": [{"key": None,
                    "code": "validation", "message": "Request body must be a JSON object."}]},
                    status_code=422)

            result = update_catalog_item(
                catalog, item_id, body.get("changes", {}), body.get("base_hash", ""), config_path,
            )
            _apply_reload_or_stage(daemon, result, f"catalog PUT {catalog}/{item_id}")
            return JSONResponse(result.model_dump(mode="json"), status_code=_save_status_code(result))


def _register_delete_config_catalog_item(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon
        config_path = ctx.config_path
        config_path = ctx.config_path

        @app.delete("/api/config/catalog/{catalog}/{item_id}")
        async def delete_config_catalog_item(catalog: str, item_id: str, request: Request) -> JSONResponse:
            """Delete a catalog item unless referenced (T031, delete-protection)."""
            from coordinare.services.config_write_service import (
                CATALOG_KEYS,
                delete_catalog_item,
            )

            if catalog not in CATALOG_KEYS:
                return JSONResponse({"ok": False, "errors": [{"key": None,
                    "code": "validation", "message": f"Unknown catalog: {catalog}"}]},
                    status_code=422)
            if config_path is None:
                return JSONResponse({"ok": False, "errors": [{"key": None,
                    "code": "forbidden", "message": "Config file not available."}]},
                    status_code=403)
            try:
                body = await request.json()
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {}

            result = delete_catalog_item(
                catalog, item_id, body.get("base_hash", ""), config_path,
            )
            _apply_reload_or_stage(daemon, result, f"catalog DELETE {catalog}/{item_id}")
            return JSONResponse(result.model_dump(mode="json"), status_code=_save_status_code(result))


def _register_get_config_routing(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon

        @app.get("/api/config/routing")
        async def get_config_routing() -> JSONResponse:
            """Return the routing table (entries + content_hash) or read-only empty state (T037)."""
            from coordinare import routing_config_service

            location = _routing_location(daemon)
            view = routing_config_service.read_routing(location)
            return JSONResponse(view.model_dump(mode="json"))


def _register_post_config_routing_entry(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon

        @app.post("/api/config/routing/entry")
        async def post_config_routing_entry(request: Request) -> JSONResponse:
            """Create a routing entry → ``applied: staged_next_job`` (T038)."""
            from coordinare import routing_config_service

            location = _routing_location(daemon)
            guard = routing_config_service._readonly_guard(location)
            if guard is not None:
                return JSONResponse(guard.model_dump(mode="json"),
                                    status_code=_save_status_code(guard))
            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"ok": False, "errors": [{"key": None,
                    "code": "validation", "message": "Invalid JSON body."}]}, status_code=422)
            if not isinstance(body, dict):
                return JSONResponse({"ok": False, "errors": [{"key": None,
                    "code": "validation", "message": "Request body must be a JSON object."}]},
                    status_code=422)

            result = routing_config_service.create_routing_entry(
                location, body.get("entry", {}), body.get("base_hash", ""),
            )
            return JSONResponse(result.model_dump(mode="json"), status_code=_save_status_code(result))


def _register_put_config_routing_entry(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon

        @app.put("/api/config/routing/entry/{index}")
        async def put_config_routing_entry(index: int, request: Request) -> JSONResponse:
            """Update the routing entry at ``index`` → ``staged_next_job`` (T039)."""
            from coordinare import routing_config_service

            location = _routing_location(daemon)
            guard = routing_config_service._readonly_guard(location)
            if guard is not None:
                return JSONResponse(guard.model_dump(mode="json"),
                                    status_code=_save_status_code(guard))
            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"ok": False, "errors": [{"key": None,
                    "code": "validation", "message": "Invalid JSON body."}]}, status_code=422)
            if not isinstance(body, dict):
                return JSONResponse({"ok": False, "errors": [{"key": None,
                    "code": "validation", "message": "Request body must be a JSON object."}]},
                    status_code=422)

            result = routing_config_service.update_routing_entry(
                location, index, body.get("changes", {}), body.get("base_hash", ""),
            )
            return JSONResponse(result.model_dump(mode="json"), status_code=_save_status_code(result))


def _register_delete_config_routing_entry(app: FastAPI, ctx: DashboardContext) -> None:
        daemon = ctx.daemon

        @app.delete("/api/config/routing/entry/{index}")
        async def delete_config_routing_entry(index: int, request: Request) -> JSONResponse:
            """Delete the routing entry at ``index`` → ``staged_next_job`` (T040)."""
            from coordinare import routing_config_service

            location = _routing_location(daemon)
            guard = routing_config_service._readonly_guard(location)
            if guard is not None:
                return JSONResponse(guard.model_dump(mode="json"),
                                    status_code=_save_status_code(guard))
            try:
                body = await request.json()
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {}

            result = routing_config_service.delete_routing_entry(
                location, index, body.get("base_hash", ""),
            )
            return JSONResponse(result.model_dump(mode="json"), status_code=_save_status_code(result))


def register(app: FastAPI, ctx: DashboardContext) -> None:
    _register_get_config_all(app, ctx)
    _register_get_config_section(app, ctx)
    _register_get_effective_config(app, ctx)
    _register_reload_config(app, ctx)
    _register_get_global_config(app, ctx)
    _register_update_global_config(app, ctx)
    _register_put_config_section(app, ctx)
    _register_get_config_catalog(app, ctx)
    _register_post_config_catalog(app, ctx)
    _register_put_config_catalog_item(app, ctx)
    _register_delete_config_catalog_item(app, ctx)
    _register_get_config_routing(app, ctx)
    _register_post_config_routing_entry(app, ctx)
    _register_put_config_routing_entry(app, ctx)
    _register_delete_config_routing_entry(app, ctx)
