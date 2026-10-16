from __future__ import annotations

import shutil
from copy import deepcopy
from pathlib import Path
from typing import Any

PRESET_NAMES = ("solo", "containers")
# Model choices match the full reference examples. Operators can replace the catalogs.
HARNESS_DEFAULTS = {
    "codex": ("openai", "OPENAI_API_KEY", "gpt-5.4"),
    "claude_code": ("anthropic", "ANTHROPIC_API_KEY", "claude-sonnet-4-6"),
    "opencode": ("openai", "OPENAI_API_KEY", "gpt-5.4"),
}
HARNESS_COMMANDS = {
    "codex": "codex",
    "claude_code": "claude",
    "opencode": "opencode",
    "opencode_compat": "opencode",
    "prime_agent": "prime-agent",
}


def _merge(defaults: dict[str, Any], explicit: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(defaults)
    for key, value in explicit.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _performer_executable() -> str:
    installed = shutil.which("performer")
    if installed:
        return installed
    checkout = Path(__file__).resolve().parents[2] / "bin" / "run-performer"
    return str(checkout) if checkout.is_file() else "performer"


def expand_preset(raw: dict[str, Any]) -> dict[str, Any]:
    """Expand a starter mapping without mutating it; explicit fields always win.

    Importable by init. Configs without a preset retain their previous defaults.
    Lists replace defaults; nested mappings merge, and an explicit null disables a role.
    """
    name = raw.get("preset")
    if name is None:
        return deepcopy(raw)
    if name not in PRESET_NAMES:
        msg = f"Unknown preset {name!r}. Set preset to solo or containers."
        raise ValueError(msg)
    harness = raw.get("harness")
    if not isinstance(harness, str) or harness not in HARNESS_DEFAULTS:
        msg = "Set harness to codex, claude_code or opencode, or use the full config reference."
        raise ValueError(msg)
    repo = raw.get("github_repo")
    if not isinstance(repo, str) or not repo.strip() or "/" in repo:
        msg = "Set github_repo to the repository name without its owner; set the owner in github_org."
        raise ValueError(msg)
    kind, credential, model = HARNESS_DEFAULTS[harness]
    defaults: dict[str, Any] = {
        "project_name": repo,
        "max_concurrent_cards": 1,
        "performers": {
            "default": {"backend": harness, "mode": "starter"},
            "implementer": {"backend": harness},
        },
        "endpoints": [{"name": "starter", "kind": kind, "auth_env": credential}],
        "model_endpoints": [{"name": "starter", "endpoint": "starter", "model": model}],
        "modes": [{"name": "starter", "strategy": "single", "tool": "starter"}],
        "env_passthrough": [credential],
    }
    if name == "solo":
        defaults["agent_executable"] = _performer_executable()
    else:
        defaults["performer_endpoints"] = [
            {
                "id": "starter",
                "mode": "ephemeral",
                "roles": ["implementer"],
                "image": raw.get("performer_image", "coordinare-performer:full"),
                "env": {"BACKEND": harness},
            },
        ]
    _validate_catalog_overrides(raw)
    return _merge(defaults, raw)


def _validate_catalog_overrides(raw: dict[str, Any]) -> None:
    """List replacement must not silently strand the starter reference chain."""
    catalogs = ("endpoints", "model_endpoints", "modes")
    for catalog in catalogs:
        if catalog not in raw:
            continue
        entries = raw[catalog]
        if not isinstance(entries, list):
            continue
        names = {entry.get("name") for entry in entries if isinstance(entry, dict)}
        if "starter" not in names and any(other not in raw for other in catalogs):
            msg = (
                f"Replacing {catalog} removes the preset's starter entry. Also override "
                "endpoints, model_endpoints and modes, and set performers.default.mode "
                "to your mode name; or keep an entry named starter in each catalog."
            )
            raise ValueError(msg)
