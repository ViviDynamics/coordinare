"""FastAPI app factory for the performer HTTP server (spec 056, T023).

Wires together:

- bearer-token auth middleware (T008 / ``server.auth``)
- capability detector (T021 / ``performer.capabilities``)
- single-slot job runner (T022 / ``server.job_runner``)

The app is a thin shell: route handlers live in ``server.routes`` and
business logic lives in ``server.job_runner`` and the executor injected
into the runner. The factory itself only owns construction and DI wiring.
"""

from __future__ import annotations

import os

from fastapi import FastAPI

from performer.capabilities import probe_capabilities
from performer.server.auth import BearerTokenAuthMiddleware
from performer.server.job_runner import JobExecutor, JobRunner
from performer.server.models import JobInitPayload, JobResult, PerformerCapabilities
from performer.server.secrets import SecretResolver, SecretSourceConfig


async def _default_executor(_: JobInitPayload) -> JobResult:
    """Placeholder executor used when none is injected.

    A real deployment wires this to the existing performer dispatch logic
    (``performer.main.handle_dispatch`` and friends). Tests inject their own.
    """
    return JobResult(
        success=False,
        summary="no executor wired",
        error_code="not_implemented",
    )


def create_app(
    *,
    expected_token: str | None = None,
    capabilities: PerformerCapabilities | None = None,
    executor: JobExecutor | None = None,
    version: str | None = None,
    resolver: SecretResolver | None = None,
) -> FastAPI:
    from performer.server.routes import register_routes

    caps = capabilities if capabilities is not None else probe_capabilities()
    runner = JobRunner(executor or _default_executor, resolver=resolver)

    app = FastAPI(title="performer", version=version or "0.0.0")
    app.add_middleware(BearerTokenAuthMiddleware, expected_token=expected_token)

    app.state.capabilities = caps
    app.state.runner = runner
    app.state.auth_enabled = expected_token is not None
    app.state.version = version

    register_routes(app)
    return app


def _bool_env(name: str, default: bool = True) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() not in ("0", "false", "no")


def create_app_from_env(
    executor: JobExecutor | None = None,
    *,
    version: str | None = None,
) -> FastAPI:
    token = os.environ.get("PERFORMER_AUTH_TOKEN") or None
    creds_file = os.environ.get("PERFORMER_CREDS_FILE") or None
    source_config = SecretSourceConfig(
        init_payload=_bool_env("PERFORMER_SECRET_SOURCE_INIT_PAYLOAD"),
        env=_bool_env("PERFORMER_SECRET_SOURCE_ENV"),
        creds_file=_bool_env("PERFORMER_SECRET_SOURCE_CREDS_FILE"),
    )
    resolver = SecretResolver(source_config, creds_file=creds_file)
    return create_app(expected_token=token, executor=executor, version=version, resolver=resolver)


__all__ = ["create_app", "create_app_from_env"]
