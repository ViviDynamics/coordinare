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


class EnvironmentCapabilityError(ValueError):
    """A trusted actionable configuration reason, never provider/source prose."""

    def __init__(self, message: str, *, code: str = "capability_unavailable") -> None:
        super().__init__(message)
        self.code = code


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
        message = "environment runtime image is unavailable"
        raise EnvironmentCapabilityError(message)
    try:
        info = json.loads(stdout)[0]
        pinned = image if "@sha256:" in image else next(iter(info.get("RepoDigests") or []), None)
        if not isinstance(pinned, str):
            message = "environment runtime image has no immutable repository digest"
            raise EnvironmentCapabilityError(message)
        immutable_verifier_image(pinned)
        os_name, architecture = info["Os"], info["Architecture"]
        machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(architecture, architecture)
    except (KeyError, TypeError, IndexError, json.JSONDecodeError) as exc:
        message = "environment runtime image identity is malformed"
        raise EnvironmentCapabilityError(message) from exc
    else:
        return pinned, f"{os_name}/{machine}"


async def resolve_execution_capabilities(state: Mapping[str, Any], symphony_name: str) -> ExecutionCapabilities:
    """Fail closed when the current target cannot supply an immutable identity."""
    config = state.get("coordinare_config")
    global_config = getattr(config, "global_config", None) or state.get("config")
    cache = (state.get("env_cache") or {}).get(symphony_name)
    if global_config is None or cache is None:
        message = "environment runtime configuration is unavailable"
        raise EnvironmentCapabilityError(message)
    transport = getattr(config, "agent_transport", None) or getattr(global_config, "agent_transport", None)
    root = "/devenv"
    if transport == "kubernetes":
        image = getattr(global_config, "kubernetes_verifier_image", None) or getattr(global_config, "performer_image", None)
        platform = getattr(global_config, "kubernetes_verifier_platform", None)
        if not platform:
            message = "environment verifier platform must be configured"
            raise EnvironmentCapabilityError(message)
        if not getattr(getattr(global_config, "env_cache", None), "kubernetes_verifier_network_isolated", False):
            message = "environment verifier network isolation is required"
            raise EnvironmentCapabilityError(message)
    else:
        image = getattr(global_config, "performer_image", None)
        # Consumer endpoints, not a separate bootstrap-only image, define the cache target.
        consumers = [endpoint for endpoint in getattr(global_config, "performer_endpoints", [])
                     if any(role != "env_bootstrap" for role in getattr(endpoint, "roles", []))]
        images = {endpoint.image for endpoint in consumers if getattr(endpoint, "image", None)}
        roots = {getattr(endpoint, "container_devenv_root", "/devenv") for endpoint in consumers}
        if len(roots) > 1:
            message = "environment consumers require one cache mount root"
            raise EnvironmentCapabilityError(message)
        if roots:
            root = roots.pop()
        if len(images) > 1:
            message = "environment consumers require one immutable target image"
            raise EnvironmentCapabilityError(message)
        if images:
            image = images.pop()
        if not image:
            message = "environment runtime image must be configured"
            raise EnvironmentCapabilityError(message)
        image, platform = await _docker_identity(image)
    target = ExecutionCapabilities(schema_version=1, image_ref=image, platform=platform,
        cache_mount_path=f"{root}/{cache.sanitised_name}", install_network="enabled", verify_network="disabled")
    await _check_bootstrap_target(state, symphony_name, target)
    return target


async def _check_bootstrap_target(state: Mapping[str, Any], symphony_name: str, target: ExecutionCapabilities) -> None:
    config = state.get("coordinare_config")
    global_config = getattr(config, "global_config", None) or state.get("config")
    transport = getattr(config, "agent_transport", None) or getattr(global_config, "agent_transport", None)
    image, platform = target.image_ref, target.platform
    symphony = (state.get("symphony_configs") or {}).get(symphony_name)
    if symphony is None:
        symphony = next((item for item in getattr(config, "symphonies", []) if item.name == symphony_name), None)
    bootstrap_id = getattr(symphony, "env_bootstrap_performer_id", None)
    if bootstrap_id:
        bootstrap = next((item for item in getattr(global_config, "performer_endpoints", []) if item.id == bootstrap_id), None)
        bootstrap_image = getattr(bootstrap, "image", None) or getattr(global_config, "performer_image", None)
        if not isinstance(bootstrap_image, str) or not bootstrap_image:
            message = "bootstrap runtime image is unavailable"
            raise EnvironmentCapabilityError(message, code="bootstrap_identity")
        if transport == "kubernetes":
            if bootstrap_image != image:
                message = "bootstrap image differs from accepted target image"
                raise EnvironmentCapabilityError(message, code="bootstrap_identity")
        elif bootstrap_image != image:
            installed_image, installed_platform = await _docker_identity(bootstrap_image)
            if installed_image != image or installed_platform != platform:
                message = "bootstrap image or platform differs from accepted target"
                raise EnvironmentCapabilityError(message, code="bootstrap_identity")


def capabilities_match(cache: EnvCacheState, capabilities: ExecutionCapabilities) -> bool:
    """An independently accepted contract cannot be reused under different constraints."""
    manifest = accepted_environment(cache)
    return manifest is not None and manifest.capabilities_digest == capabilities.digest()


def installer_capabilities(cache: EnvCacheState) -> ExecutionCapabilities:
    target = ExecutionCapabilities.model_validate(cache.environment_capabilities)
    if target.digest() != (cache.environment_manifest or {}).get("capabilities_digest"):
        message = "accepted environment runtime identity differs from installer target"
        raise ValueError(message)
    return target


def require_cache_mount(capabilities: ExecutionCapabilities, mount_path: str) -> None:
    from coordinare.services.environment_attempt import EnvironmentDiscoveryError

    if capabilities.cache_mount_path != mount_path:
        message = "environment cache mount differs from runtime capabilities"
        raise EnvironmentDiscoveryError(message)
