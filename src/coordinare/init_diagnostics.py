from __future__ import annotations

import errno
import os
from contextvars import ContextVar
from typing import Literal

import httpx

from coordinare.init_files import SetupError

InitStage = Literal[
    "answers", "github", "board", "harness", "docker_runtime", "docker_disk",
    "image_pull", "image_inspect", "container_setup", "container_smoke", "model_endpoint",
    "write_config", "preflight", "service",
]
# Per invocation, shared by synchronous helpers without changing their public APIs.
init_stage: ContextVar[InitStage] = ContextVar("init_stage", default="answers")
DOCKER_DISK_FIX = "Free disk space on the Docker filesystem or increase its disk allocation; review usage with docker system df, then rerun init."
STAGE_FIXES: dict[InitStage, str] = {
    "answers": "Check the answers YAML, required values and input file permissions, then rerun init.",
    "github": "Check GitHub connectivity, token scopes and repository access, then rerun init.",
    "board": "Check Projects access, the board number and lifecycle Status columns, then rerun init.",
    "harness": "Install and authenticate the selected harness CLI, then rerun init.",
    "docker_runtime": "Check docker info, Docker service status and your Docker context and access, then rerun init.",
    "docker_disk": DOCKER_DISK_FIX,
    "image_pull": "Check registry access, the release tag, Docker network settings and disk space, then rerun init.",
    "image_inspect": "Pull the selected release again and check docker image inspect, then rerun init.",
    "container_setup": "Check free disk space and permissions on the performer workspace, artifact and cache directories, then rerun init.",
    "container_smoke": "Check Docker disk space, harness readiness and container GitHub/model connectivity, then rerun init.",
    "model_endpoint": "Check the harness, model, endpoint and provider credentials in your setup answers, then rerun init.",
    "write_config": "Check free disk space and permissions on the configuration directory, then rerun init.",
    "preflight": "Repair the reported preflight checks and verify with coordinare doctor before starting a service.",
    "service": "Check your user login session and retry coordinare service install with the saved configuration.",
}


def safe_cause(exc: Exception) -> str:
    """Use structured metadata only; never messages, full URLs or filenames."""
    name = type(exc).__name__
    if isinstance(exc, SetupError) and exc.cause:
        return f"{name} ({exc.cause})"
    if isinstance(exc, OSError) and exc.errno is not None and exc.errno in errno.errorcode:
        return f"{name} ({errno.errorcode[exc.errno]}: {os.strerror(exc.errno)})"
    if isinstance(exc, (httpx.RequestError, httpx.HTTPStatusError)):
        metadata = []
        if isinstance(exc, httpx.HTTPStatusError):
            metadata.append(f"status={exc.response.status_code}")
        try:
            # URL.host excludes userinfo, path and query, unlike the exception text.
            host = exc.request.url.host
        except RuntimeError:
            # RequestError can be raised before a request has been attached.
            host = ""
        if host:
            metadata.append(f"host={host}")
        if metadata:
            return f"{name} ({', '.join(metadata)})"
    return name


def render_failure(exc: Exception) -> str:
    stage = init_stage.get()
    fix = STAGE_FIXES[stage]
    detail = ""
    if isinstance(exc, SetupError):
        # Only explicitly controlled SetupError text is displayable.
        detail = str(exc) + "\n"
        if exc.fix:
            return f"Setup failed at {stage}: {safe_cause(exc)}.\n{detail}"
    elif isinstance(exc, OSError) and exc.errno == errno.ENOSPC and stage in {
        "docker_runtime", "docker_disk", "image_pull", "image_inspect", "container_smoke",
    }:
        fix = DOCKER_DISK_FIX
    return f"Setup failed at {stage}: {safe_cause(exc)}.\n{detail}Fix: {fix}\n"
