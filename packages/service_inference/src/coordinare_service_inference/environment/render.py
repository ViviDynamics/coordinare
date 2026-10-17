"""Render trusted wrappers from the authoritative manifest, never writable scripts."""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

from .models import environment_order

if TYPE_CHECKING:
    from .models import EnvironmentManifest

_REFERENCE = re.compile(r"(\{cache\}|\{repo\}|\{scratch\}|\$\{[A-Za-z_][A-Za-z0-9_]*\})")


def _shell_value(value: str, cache: str) -> str:
    parts = []
    for part in _REFERENCE.split(value):
        if not part:
            continue
        if part == "{cache}":
            parts.append(shlex.quote(cache))
        elif part == "{repo}":
            parts.append('"${COORDINARE_REPO_DIR:-$PWD}"')
        elif part == "{scratch}":
            parts.append('"${COORDINARE_SCRATCH_DIR:-/tmp/coordinare-environment}"')
        elif part.startswith("${"):
            parts.append('"${' + part[2:-1] + ':?required environment reference missing}"')
        else:
            parts.append(shlex.quote(part))
    return "".join(parts) or "''"


def render_activate(manifest: EnvironmentManifest, *, cache_mount_path: str) -> str:
    lines = ["# Declarative activation for manifest " + manifest.digest()]
    for key in environment_order(manifest.activation.env):
        lines.append(f"export {key}={_shell_value(manifest.activation.env[key], cache_mount_path)}")
    # Reverse insertion preserves declaration order while making repeated sourcing stable.
    for path in reversed(manifest.activation.path):
        lines.extend(
            [
                "_coordinare_path=" + _shell_value(path, cache_mount_path),
                'case ":${PATH-}:" in',
                '  *":${_coordinare_path}:"*) ;;',
                '  *) export PATH="${_coordinare_path}${PATH:+:${PATH}}" ;;',
                "esac",
            ],
        )
    lines.append("unset _coordinare_path")
    return "\n".join(lines) + "\n"


def render_verify(manifest: EnvironmentManifest, *, cache_mount_path: str) -> str:
    return "\n".join(
        [
            "#!/bin/bash",
            "set -eu",
            "unset BASH_ENV ENV",
            "_coordinare_scratch=$(mktemp -d)",
            "trap 'rm -rf -- \"$_coordinare_scratch\"' EXIT",
            # The interpreter/module belongs to the trusted image; activation is applied by Python.
            "/usr/local/bin/python -I -m coordinare_service_inference.environment.runner "
            + "--manifest-stdin"
            + " --cache "
            + shlex.quote(cache_mount_path)
            + ' --repo "${COORDINARE_REPO_DIR:-$PWD}"'
            + ' --scratch "$_coordinare_scratch"'
            + " --scope \"${COORDINARE_ENV_SCOPE:-}\" <<'COORDINARE_MANIFEST_351'",
            manifest.canonical_json(),
            "COORDINARE_MANIFEST_351",
            "",
        ],
    )
