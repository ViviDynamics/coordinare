"""Resolve real runtime constraints before asking agents to propose an environment."""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from coordinare_service_inference.environment.models import ExecutionCapabilities

from coordinare.services.environment_acceptance import (
    accepted_environment,
    immutable_verifier_image,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from coordinare.models.env_cache import EnvCacheState


async def _docker_identity(image: str) -> tuple[str, str]:
    """Inspect local content identity without pulling or executing project code."""
    proc = await asyncio.create_subprocess_exec("docker", "image", "inspect", image,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        raise ValueError("environment runtime image is unavailable")
    try:
        info = json.loads(stdout)[0]
        pinned = image if "@sha256:" in image else next(iter(info.get("RepoDigests") or []), None)
        if not isinstance(pinned, str):
            raise ValueError("environment runtime image has no immutable repository digest")
        immutable_verifier_image(pinned)
        os_name, architecture = info["Os"], info["Architecture"]
        machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(architecture, architecture)
        return pinned, f"{os_name}/{machine}"
    except (KeyError, TypeError, IndexError, json.JSONDecodeError) as exc:
        raise ValueError("environment runtime image identity is malformed") from exc


async def resolve_execution_capabilities(state: Mapping[str, Any], symphony_name: str) -> ExecutionCapabilities:
    """Fail closed when the current target cannot supply an immutable identity."""
    config = state.get("coordinare_config")
    global_config = getattr(config, "global_config", None) or state.get("config")
    cache = (state.get("env_cache") or {}).get(symphony_name)
    if global_config is None or cache is None:
        raise ValueError("environment runtime configuration is unavailable")
    transport = getattr(config, "agent_transport", None) or getattr(global_config, "agent_transport", None)
    root = "/devenv"
    if transport == "kubernetes":
        image = getattr(global_config, "kubernetes_verifier_image", None) or getattr(global_config, "performer_image", None)
        platform = getattr(global_config, "kubernetes_verifier_platform", None)
        if not platform:
            raise ValueError("environment verifier platform must be configured")
        if not getattr(getattr(global_config, "env_cache", None), "kubernetes_verifier_network_isolated", False):
            raise ValueError("environment verifier network isolation is required")
    else:
        image = getattr(global_config, "performer_image", None)
        # Consumer endpoints, not a separate bootstrap-only image, define the cache target.
        consumers = [endpoint for endpoint in getattr(global_config, "performer_endpoints", [])
                     if any(role != "env_bootstrap" for role in getattr(endpoint, "roles", []))]
        images = {endpoint.image for endpoint in consumers if getattr(endpoint, "image", None)}
        roots = {getattr(endpoint, "container_devenv_root", "/devenv") for endpoint in consumers}
        if len(roots) > 1:
            raise ValueError("environment consumers require one cache mount root")
        if roots:
            root = roots.pop()
        if len(images) > 1:
            raise ValueError("environment consumers require one immutable target image")
        if images:
            image = images.pop()
        if not image:
            raise ValueError("environment runtime image must be configured")
        image, platform = await _docker_identity(image)
    return ExecutionCapabilities(schema_version=1, image_ref=image, platform=platform,
        cache_mount_path=f"{root}/{cache.sanitised_name}", install_network="enabled", verify_network="disabled")


def capabilities_match(cache: EnvCacheState, capabilities: ExecutionCapabilities) -> bool:
    """An independently accepted contract cannot be reused under different constraints."""
    manifest = accepted_environment(cache)
    return manifest is not None and manifest.capabilities_digest == capabilities.digest()
