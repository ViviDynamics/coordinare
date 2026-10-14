"""A stand-in for the loaded global config, enough for the performer-mount list (219 review).

Env-cache reads take ``performer_mount_hosts(global_config)``, which reads the log dir, the
endpoint volumes and the env-cache root. Tests that stub the config with a namespace use this
so the mount list resolves without a full ProjectConfiguration.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def mount_global(**extra: Any) -> SimpleNamespace:
    """A global-config stand-in with no performer volumes and a root that exists nowhere."""
    fields: dict[str, Any] = {
        "performer_log_dir": None,
        "performer_endpoints": [],
        "env_cache_root": "/nonexistent/env-caches",
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


def mount_config(**global_extra: Any) -> SimpleNamespace:
    """A coordinare-config stand-in whose ``global_config`` is :func:`mount_global`."""
    return SimpleNamespace(global_config=mount_global(**global_extra))
