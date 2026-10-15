"""Unset ``${NAME}`` placeholders in environment values, and the entries the daemon leaves out (252).

An environment value written as ``${NAME}`` in config.yaml is expanded from the daemon's environment.
When NAME is not set, expansion leaves the literal text, and passing that text on makes an unset
variable look set. So an unset placeholder is never passed to a performer. Whether it is also a
refusal depends on the key: a secret-looking key is refused at boot and on save, and any other key
is left out of the launch with a warning.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = structlog.get_logger(__name__)

_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: A key that looks secret: one of these words as a whole ``_``-separated segment, in any case. A
#: whole segment, so a tuning knob such as ``COORDINARE_INFERENCE_MAX_TOKENS`` is not secret, while
#: ``API_KEY``, ``GITHUB_TOKEN`` or ``LITELLM_MASTER_KEY`` are. A secret the pattern misses is only
#: left out of the launch with a warning, never passed as the literal placeholder.
ENV_SECRET_KEY = re.compile(
    r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD|PASS|CREDENTIAL|AUTH)(?:_|$)",
    re.IGNORECASE,
)


def is_secret_env_key(key: str) -> bool:
    """True when an environment key looks secret (see :data:`ENV_SECRET_KEY`)."""
    return ENV_SECRET_KEY.search(key) is not None


def unset_placeholder_names(value: object) -> list[str]:
    """The NAMEs of the ``${NAME}`` placeholders in ``value`` that are not set in the environment."""
    if not isinstance(value, str):
        return []
    return [match.group(1) for match in _REFERENCE.finditer(value) if match.group(1) not in os.environ]


def drop_unset_placeholders(env: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """Split ``env`` into the entries to pass on and the keys to leave out (an unset placeholder)."""
    kept: dict[str, str] = {}
    dropped: list[str] = []
    for key, value in env.items():
        if unset_placeholder_names(value):
            dropped.append(key)
        else:
            kept[key] = value
    return kept, dropped


def operator_workflow_env(role: Any) -> dict[str, str]:
    """A role's ``workflow_env`` as it is passed on, with unset placeholders left out (252)."""
    kept, dropped = drop_unset_placeholders(dict(getattr(role, "workflow_env", None) or {}))
    for key in dropped:
        logger.info("workflow_env.placeholder_omitted", env_key=key)
    return kept
