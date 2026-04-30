"""Secret resolver for performer job execution (spec 056, T057–T059).

Implements a three-source resolver with deterministic precedence:
1. init_payload (job-init secrets from coordinare)
2. env (container environment variables)
3. creds_file (mounted credentials file or directory)

Per-source enable/disable via SecretSourceConfig allows operators to opt out
of particular sources. Missing required secrets raise SecretMissingError with
only the secret name (never the value) for safe error reporting.

Secret logging follows FR-023: never emit any secret value at any level,
including in error messages.
"""

from __future__ import annotations

import os
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


class SecretMissingError(RuntimeError):
    """Raised when a required secret cannot be resolved from any source.

    Attributes:
        name: The secret key name (safe to log).
    """

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"required secret missing: {name}")


class SecretSourceConfig:
    """Per-performer configuration for which secret sources to use."""

    __slots__ = ("init_payload", "env", "creds_file")

    def __init__(
        self,
        init_payload: bool = True,
        env: bool = True,
        creds_file: bool = True,
    ) -> None:
        self.init_payload = init_payload
        self.env = env
        self.creds_file = creds_file


class SecretResolver:
    """Resolves secrets from three sources with deterministic precedence.

    Precedence order:
    1. init_payload — secrets provided at job dispatch
    2. env — container environment variables
    3. creds_file — mounted credentials file (KEY=VALUE format) or directory
                   (each file is a secret; contents is the value)

    Each source can be independently enabled/disabled via SecretSourceConfig.
    Missing secrets raise SecretMissingError referencing only the secret name.
    """

    def __init__(
        self,
        config: SecretSourceConfig,
        *,
        init_secrets: dict[str, str | None] | None = None,
        creds_file: Path | str | None = None,
    ) -> None:
        """Initialize the resolver.

        Args:
            config: Per-source enable/disable flags
            init_secrets: Secrets from JobInitPayload.secrets dict
            creds_file: Path to a credentials file (KEY=VALUE) or directory
                        (each file name is a key; file contents is the value)
        """
        self.config = config
        self.init_secrets = init_secrets or {}
        self.creds_file = Path(creds_file) if creds_file else None
        self._creds_cache: dict[str, str] | None = None

    def resolve(self, name: str) -> str:
        """Resolve a secret by name.

        Precedence:
        1. If init_payload is enabled, check init_secrets (skip None values)
        2. If env is enabled, check os.environ
        3. If creds_file is enabled, load from file

        Args:
            name: Secret key name (e.g., "GITHUB_TOKEN")

        Returns:
            The resolved secret value.

        Raises:
            SecretMissingError: If secret is not found in any enabled source.
        """
        # 1. Check init_payload
        if self.config.init_payload:
            if name in self.init_secrets:
                value = self.init_secrets[name]
                if value is not None:
                    log.info("secret_resolved", source="init_payload", name=name)
                    return value

        # 2. Check env
        if self.config.env:
            value = os.environ.get(name)
            if value is not None:
                log.info("secret_resolved", source="env", name=name)
                return value

        # 3. Check creds_file
        if self.config.creds_file:
            value = self._resolve_from_creds(name)
            if value is not None:
                log.info("secret_resolved", source="creds_file", name=name)
                return value

        # Not found in any enabled source
        log.warning("secret_missing", name=name)
        raise SecretMissingError(name)

    def _resolve_from_creds(self, name: str) -> str | None:
        """Load secret from creds_file.

        Supports two formats:
        - Single file with KEY=VALUE lines
        - Directory where each file name is a key and contents is the value

        Args:
            name: Secret key name

        Returns:
            Secret value if found, None otherwise.
        """
        if self.creds_file is None:
            return None

        if not self.creds_file.exists():
            return None

        # Load creds into cache if not already cached
        if self._creds_cache is None:
            self._creds_cache = self._load_creds_file()

        return self._creds_cache.get(name)

    def _load_creds_file(self) -> dict[str, str]:
        """Load credentials from file or directory.

        Returns:
            Dictionary mapping secret names to values.
        """
        creds = {}

        if self.creds_file is None:
            return creds

        if self.creds_file.is_dir():
            # Directory mode: each file name is a secret key.
            # is_file() already follows symlinks, so Kubernetes secret volume
            # mounts (which use symlinks) are resolved correctly.
            for filepath in self.creds_file.iterdir():
                if filepath.is_file():
                    if filepath.name.startswith("."):
                        continue
                    key = filepath.name
                    value = filepath.read_text(encoding="utf-8").rstrip("\n")
                    creds[key] = value
        elif self.creds_file.is_file():
            # File mode: KEY=VALUE lines
            content = self.creds_file.read_text(encoding="utf-8")
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                creds[key.strip()] = value.strip()

        return creds


__all__ = ["SecretResolver", "SecretSourceConfig", "SecretMissingError"]
