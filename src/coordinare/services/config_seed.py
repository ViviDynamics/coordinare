from __future__ import annotations

import argparse
import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
import yaml

from coordinare.services.config_ownership import (
    ENDPOINT_IMAGE_KEY,
    SEED_ENV,
    chart_owned_keys,
    endpoint_image_pins,
    user_config,
)
from coordinare.services.config_secret_policy import persistent_secret_problem
from coordinare.services.config_write_service import atomic_write_yaml

_METADATA = "seed.yaml"
_RECOVERY = "recovery.yaml"
_RESET = "reseed"
_HASH_PREFIX = "sha256:"
_HASH_HEX_LENGTH = 64
_log = structlog.get_logger(__name__)


def _read_document(path: Path) -> tuple[dict[str, Any], str]:
    content = path.read_bytes()
    loaded = yaml.safe_load(content)
    if not isinstance(loaded, dict) or not all(isinstance(key, str) for key in loaded):
        message = "Config must be a YAML mapping with string keys"
        raise ValueError(message)
    return loaded, _HASH_PREFIX + hashlib.sha256(content).hexdigest()


def _sections(raw: dict[str, Any], chart: dict[str, Any] | None = None) -> dict[str, str]:
    """Fingerprint parsed sections, excluding the chart's runtime contract."""
    return {
        key: hashlib.sha256(yaml.safe_dump(value, sort_keys=True).encode()).hexdigest()
        for key, value in user_config(raw, chart).items()
    }


def _changed(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    return sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))


def _overlay(raw: dict[str, Any], live_raw: dict[str, Any]) -> dict[str, Any]:
    candidate = dict(live_raw)
    for key in sorted(chart_owned_keys(raw) - {ENDPOINT_IMAGE_KEY}):
        if key in raw:
            candidate[key] = raw[key]
        else:
            candidate.pop(key, None)
        _log.info("config.chart_overlay", key=key, present=key in raw)
    pins = endpoint_image_pins(raw)
    endpoints = candidate.get("performer_endpoints")
    if isinstance(endpoints, list):
        candidate["performer_endpoints"] = [
            {**item, "image": pins[item["id"]]}
            if isinstance(item, dict) and item.get("id") in pins
            else item
            for item in endpoints
        ]
        for endpoint_id in sorted(pins):
            _log.info(
                "config.chart_overlay", key=f"performer_endpoints.{endpoint_id}.image", present=True,
            )
    return candidate


def _recover_broken(live: Path) -> None:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup = live.with_name(f"{live.name}.broken-{stamp}")
    live.rename(backup)
    atomic_write_yaml(
        live.parent / _RECOVERY, {"backup": backup.name, "at": datetime.now(UTC).isoformat()},
    )
    _log.error(
        "config.live_file_recovered",
        backup=backup.name,
        message="Live config was empty or invalid YAML; preserved it and restored the chart seed.",
    )


def seed_config(seed: Path, live: Path) -> None:
    """Seed missing or broken YAML; overlay chart fields on every Pod boot.

    Parseable mappings never get auto-replaced for failing validation or start
    checks. Failed explicit resets retain their marker. No env expansion occurs.
    """
    raw, seed_hash = _read_document(seed)
    marker = live.parent / _RESET
    if live.exists() and not marker.exists():
        try:
            live_raw, _ = _read_document(live)
        except (UnicodeError, ValueError, yaml.YAMLError):
            problem = persistent_secret_problem(raw)
            if problem:
                raise ValueError(problem) from None
            _recover_broken(live)
        else:
            candidate = _overlay(raw, live_raw)
            if candidate != live_raw:
                atomic_write_yaml(live, candidate)
            return
    problem = persistent_secret_problem(raw)
    if problem:
        raise ValueError(problem)
    atomic_write_yaml(live, _overlay(raw, raw))
    atomic_write_yaml(live.parent / _METADATA, {"seed_hash": seed_hash, "sections": _sections(raw)})
    marker.unlink(missing_ok=True)


def _is_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _HASH_HEX_LENGTH
        and all(char in "0123456789abcdef" for char in value)
    )


def _valid_metadata(raw: dict[str, Any]) -> bool:
    digest = raw.get("seed_hash")
    sections = raw.get("sections")
    return (
        isinstance(digest, str)
        and digest.startswith(_HASH_PREFIX)
        and _is_digest(digest.removeprefix(_HASH_PREFIX))
        and isinstance(sections, dict)
        and all(isinstance(key, str) and _is_digest(value) for key, value in sections.items())
    )


def recovery_status(live: Path | None) -> dict[str, Any] | None:
    if live is None:
        return None
    try:
        raw, _ = _read_document(live.parent / _RECOVERY)
        backup, at = raw.get("backup"), raw.get("at")
        if (
            isinstance(backup, str)
            and backup.startswith(f"{live.name}.broken-")
            and Path(backup).name == backup
            and isinstance(at, str)
        ):
            return {"backup": backup, "at": at}
    except (OSError, UnicodeError, ValueError, yaml.YAMLError):
        return None
    return None


def recovery_health_row(live: Any) -> dict[str, Any] | None:
    recovery = recovery_status(live if isinstance(live, Path) else None)
    if recovery is None:
        return None
    return {
        "key": "config_recovery",
        "name": "Config recovery",
        "status": "degraded",
        "summary": "Recovered an empty or corrupt live config from the chart seed.",
        "reason": f"Inspect {recovery['backup']} on the state PVC. Dashboard edits may need restoring.",
        "checked_at": recovery["at"],
    }


def seed_status(seed: Path, live: Path) -> dict[str, Any]:
    """Value-free semantic drift, with a persistent recovery notice."""
    status: dict[str, Any] = {
        "chart_changed": None,
        "changed_sections": [],
        "live_diff_sections": [],
        "recovery": recovery_status(live),
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
        status["live_diff_sections"] = _changed(_sections(live_raw, current_raw), current)
        if not _valid_metadata(baseline):
            return status
        recorded = {
            key: value
            for key, value in baseline["sections"].items()
            if key not in chart_owned_keys(current_raw)
        }
        if baseline["seed_hash"] == current_hash and recorded != current:
            return status
        status["changed_sections"] = _changed(recorded, current)
        status["chart_changed"] = bool(status["changed_sections"])
        status["message"] = (
            "chart values changed since this config was seeded"
            if status["chart_changed"]
            else "Chart seed is unchanged since seeding."
        )
    except (OSError, UnicodeError, ValueError, yaml.YAMLError):
        return status
    return status


def acknowledge_sections(
    live: Path, sections: list[str], raw: dict[str, Any], seed_hash: str,
) -> None:
    """Advance provenance for selected sections only; retain unadopted drift."""
    current = _sections(raw)
    try:
        baseline, _ = _read_document(live.parent / _METADATA)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError):
        baseline = {}
    recorded = dict(baseline.get("sections", {})) if _valid_metadata(baseline) else {}
    for section in sections:
        if section in current:
            recorded[section] = current[section]
        else:
            recorded.pop(section, None)
    # A partial adoption is a new baseline assembled from more than one seed.
    provenance_hash = seed_hash if recorded == current else baseline.get("seed_hash", seed_hash)
    atomic_write_yaml(live.parent / _METADATA, {"seed_hash": provenance_hash, "sections": recorded})


def configured_seed_status(live: Path | None) -> dict[str, Any] | None:
    seed = os.environ.get(SEED_ENV)
    return seed_status(Path(seed), live) if seed and live is not None else None


def log_seed_status(live: Path | None) -> None:
    status = configured_seed_status(live)
    if status is not None:
        _log.info("config.seed_status", **status)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed persistent config and overlay chart fields")
    parser.add_argument("seed", type=Path)
    parser.add_argument("live", type=Path)
    args = parser.parse_args()
    try:
        seed_config(args.seed, args.live)
    except (OSError, UnicodeError, ValueError, yaml.YAMLError):
        parser.exit(
            1, "Config seed failed. Check mount permissions, YAML and secret placeholders.\n",
        )


if __name__ == "__main__":
    main()
