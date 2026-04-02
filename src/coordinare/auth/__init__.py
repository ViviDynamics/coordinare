"""GitHub authentication layer — PAT and App auth implementations."""
from __future__ import annotations

import sys

from coordinare.auth.app import AppAuth
from coordinare.auth.pat import PatAuth
from coordinare.auth.protocol import GitHubAuth

__all__ = ["AppAuth", "GitHubAuth", "PatAuth", "build_auth", "validate_auth_config"]


def build_auth(config: object) -> GitHubAuth:
    """Construct the correct GitHubAuth implementation from ProjectConfiguration."""
    if getattr(config, "github_auth", "pat") == "pat":
        token_field = getattr(config, "github_token", None)
        token = token_field.get_secret_value() if token_field else ""
        return PatAuth(token)

    if getattr(config, "github_auth", None) == "app":
        return AppAuth(
            app_id=config.github_app_id,  # type: ignore[union-attr]
            private_key_path=config.github_private_key_path,  # type: ignore[union-attr]
            installation_id=config.github_installation_id,  # type: ignore[union-attr]
            api_url=getattr(config, "github_api_url", "https://api.github.com"),
        )

    msg = f"Unsupported github_auth mode: {getattr(config, 'github_auth', None)!r}"
    raise ValueError(msg)


def validate_auth_config(config: object) -> None:
    """Verify auth config is usable at startup; SystemExit with actionable message on failure."""
    if getattr(config, "github_auth", None) != "app":
        return
    path = getattr(config, "github_private_key_path", None)
    if path is None:
        return
    if not path.exists():
        print(
            f"ERROR: github_private_key_path {str(path)!r} does not exist. "
            "Create the file or correct the path in your config.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not path.is_file():
        print(
            f"ERROR: github_private_key_path {str(path)!r} is not a regular file.",
            file=sys.stderr,
        )
        sys.exit(1)
