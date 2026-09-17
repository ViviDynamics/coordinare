"""Config file discovery: 4-path search order for coordinare config files (008)."""
from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "ConfigDiscoveryError",
    "discover_config_path",
]


class ConfigDiscoveryError(Exception):
    """Raised when an explicitly specified config path cannot be used."""


def discover_config_path(explicit: Path | None) -> Path | None:
    """Return the resolved config file path using the 4-path priority order.

    Priority:
    1. explicit  (from --config flag) — must exist and be a file
    2. COORDINARE_CONFIG_PATH env var  — must exist and be a file
    3. ./config.yaml                  — current working directory
    4. ~/.coordinare/config.yaml       — user home directory
    5. None                           — no file found; env-vars-only startup

    Raises ConfigDiscoveryError if an explicitly specified path (explicit arg
    or COORDINARE_CONFIG_PATH env var) does not exist or is a directory.
    """
    # 1. Explicit path from --config flag
    if explicit is not None:
        p = Path(explicit)
        if not p.exists():
            raise ConfigDiscoveryError(f"Config file not found: {p}")
        if p.is_dir():
            raise ConfigDiscoveryError(
                f"Config path must point to a file, not a directory: {p}",
            )
        return p.resolve()

    # 2. COORDINARE_CONFIG_PATH environment variable
    env_path_str = os.environ.get("COORDINARE_CONFIG_PATH")
    if env_path_str:
        env_path = Path(env_path_str)
        if not env_path.exists():
            raise ConfigDiscoveryError(f"Config file not found: {env_path}")
        if env_path.is_dir():
            raise ConfigDiscoveryError(
                f"Config path must point to a file, not a directory: {env_path}",
            )
        return env_path.resolve()

    # 3. Working directory
    cwd_path = Path.cwd() / "config.yaml"
    if cwd_path.is_file():
        return cwd_path.resolve()

    # 4. User home directory
    home_path = Path.home() / ".coordinare" / "config.yaml"
    if home_path.is_file():
        return home_path.resolve()

    # 5. No file found — caller handles env-vars-only case
    return None
