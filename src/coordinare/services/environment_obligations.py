"""Deterministic preservation of established environment obligations."""
from __future__ import annotations

from typing import TYPE_CHECKING

from coordinare.services.environment_attempt import EnvironmentDiscoveryError

if TYPE_CHECKING:
    from coordinare_service_inference.environment.models import EnvironmentManifest


def preserve_obligations(manifest: EnvironmentManifest, previous: EnvironmentManifest) -> None:
    """A fresh judgment can add coverage, never waive a previously required obligation."""
    for group in ("requirements", "install", "checks", "services"):
        current = {item.id: item for item in getattr(manifest, group) if item.required}
        for old in getattr(previous, group):
            if not old.required:
                continue
            new = current.get(old.id)
            if new is None:
                message = "required obligation removed during repair"
                raise EnvironmentDiscoveryError(message, code="obligation_removed")
            if group == "requirements" and not set(old.checks) <= set(new.checks):
                message = "required obligation coverage removed"
                raise EnvironmentDiscoveryError(message, code="obligation_removed")
            if group in {"checks", "install"} and old.scope != new.scope:
                message = "required obligation scope changed"
                raise EnvironmentDiscoveryError(message, code="obligation_removed")
            if group == "services":
                for phase in ("init", "start", "health", "stop"):
                    before, after = getattr(old, phase), getattr(new, phase)
                    if before is not None and (after is None or before.id != after.id or before.scope != after.scope
                                               or (before.required and not after.required)):
                        message = "required service obligation changed"
                        raise EnvironmentDiscoveryError(message, code="obligation_removed")

