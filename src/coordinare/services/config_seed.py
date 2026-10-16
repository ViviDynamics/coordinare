from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
from typing import Any

import structlog
import yaml

from coordinare.services.config_secret_policy import persistent_secret_problem
from coordinare.services.config_write_service import atomic_write_yaml

_METADATA = "seed.yaml"
_RESET = "reseed"
_SEED_ENV = "COORDINARE_CONFIG_SEED"
_HASH_PREFIX = "sha256:"
_HASH_HEX_LENGTH = 64


def _read_document(path: Path) -> tuple[dict[str, Any], str]:
    content = path.read_bytes()
    loaded = yaml.safe_load(content)
    if not isinstance(loaded, dict) or not all(isinstance(key, str) for key in loaded):
        raise ValueError("Config seed must be a YAML mapping with string keys")
    return loaded, _HASH_PREFIX + hashlib.sha256(content).hexdigest()


def _sections(raw: dict[str, Any]) -> dict[str, str]:
    """Fingerprint sections without retaining a second copy of configuration."""
    return {
        key: hashlib.sha256(yaml.safe_dump(value, sort_keys=True).encode()).hexdigest()
        for key, value in raw.items()
    }


def _changed(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    return sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))


def seed_config(seed: Path, live: Path) -> None:
    """Import once, or explicitly reset, before the daemon starts on its state PVC.

    A present file always wins, including when provenance metadata was lost.
    Failed resets retain their marker. No environment expansion occurs here.
    """
    marker = live.parent / _RESET
    if live.exists() and not marker.exists():
        return
    raw, seed_hash = _read_document(seed)
    problem = persistent_secret_problem(raw)
    if problem:
        raise ValueError(problem)
    atomic_write_yaml(live, raw)
    atomic_write_yaml(live.parent / _METADATA, {"seed_hash": seed_hash, "sections": _sections(raw)})
    marker.unlink(missing_ok=True)


def _is_digest(value: Any) -> bool:
    return (
        isinstance(value, str) and len(value) == _HASH_HEX_LENGTH
        and all(char in "0123456789abcdef" for char in value)
    )


def _valid_metadata(raw: dict[str, Any]) -> bool:
    digest = raw.get("seed_hash")
    sections = raw.get("sections")
    if not isinstance(digest, str) or not digest.startswith(_HASH_PREFIX):
        return False
    suffix = digest.removeprefix(_HASH_PREFIX)
    return (
        _is_digest(suffix)
        and isinstance(sections, dict)
        and all(isinstance(key, str) and _is_digest(value) for key, value in sections.items())
    )


def seed_status(seed: Path, live: Path) -> dict[str, Any]:
    """Return a value-free drift summary suitable for authenticated Config reads."""
    status: dict[str, Any] = {
        "chart_changed": None,
        "changed_sections": [],
        "live_diff_sections": [],
        "message": "Config seed provenance is unknown; inspect the seed and live config before resetting.",
        "reset_instruction": (
            "Back up the live config, create config/reseed on the state PVC, then restart the Pod "
            "to adopt the current chart seed and discard dashboard edits."
        ),
    }
    try:
        baseline, _ = _read_document(live.parent / _METADATA)
        current_raw, current_hash = _read_document(seed)
        live_raw, _ = _read_document(live)
        current = _sections(current_raw)
        status["live_diff_sections"] = _changed(_sections(live_raw), current)
        if not _valid_metadata(baseline):
            return status
        if baseline["seed_hash"] == current_hash and baseline["sections"] != current:
            return status
        status["chart_changed"] = baseline["seed_hash"] != current_hash
        status["changed_sections"] = _changed(baseline["sections"], current)
        status["message"] = (
            "chart values changed since this config was seeded"
            if status["chart_changed"] else "Chart seed is unchanged since seeding."
        )
    except (OSError, UnicodeError, ValueError, yaml.YAMLError):
        return status
    return status


def configured_seed_status(live: Path | None) -> dict[str, Any] | None:
    """Opt in only for the Helm persistent config deployment."""
    seed = os.environ.get(_SEED_ENV)
    return seed_status(Path(seed), live) if seed and live is not None else None


def log_seed_status(live: Path | None) -> None:
    """Compare at boot without putting config values or paths in the log."""
    status = configured_seed_status(live)
    if status is not None:
        structlog.get_logger(__name__).info("config.seed_status", **status)


def main() -> None:
    """Seed entry point for the chart init container, using the daemon image."""
    parser = argparse.ArgumentParser(description="Seed persistent config once, or honor a reseed marker")
    parser.add_argument("seed", type=Path)
    parser.add_argument("live", type=Path)
    args = parser.parse_args()
    try:
        seed_config(args.seed, args.live)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError):
        # YAML parser exceptions can contain source lines. Keep credentials out of logs.
        parser.exit(1, "Config seed failed. Check mount permissions, YAML and secret placeholders.\n")


if __name__ == "__main__":
    main()
