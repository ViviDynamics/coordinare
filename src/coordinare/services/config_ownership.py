"""The chart's runtime contract, shared by boot, drift and dashboard writes."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ENDPOINT_IMAGE_KEY = "performer_endpoints.*.image"
CHART_OWNED_KEYS = (
    "dashboard_host",
    "dashboard_port",
    "health_check_port",
    "state_file_path",
    "env_cache_root",
    "kubernetes_cache_claim",
    "kubernetes_namespace",
    "trusted_dashboard_hosts",
    "observability",
    "agent_transport",
    "kubernetes_owner",
    "kubernetes_verifier_image",
    "kubernetes_image_pull_secrets",
    "github_private_key_path",
    "performer_image",
    ENDPOINT_IMAGE_KEY,
)
CHART_MANAGED_LABEL = "managed by the Helm chart"
SEED_ENV = "COORDINARE_CONFIG_SEED"


def chart_owned_keys(seed: dict[str, Any]) -> set[str]:
    """Image pins and the private-key path are owned only when supplied."""
    return {
        key
        for key in CHART_OWNED_KEYS
        if (key not in {"performer_image", "github_private_key_path"} or key in seed)
        and (key != ENDPOINT_IMAGE_KEY or endpoint_image_pins(seed))
    }


def endpoint_image_pins(raw: dict[str, Any]) -> dict[str, str]:
    endpoints = raw.get("performer_endpoints")
    if not isinstance(endpoints, list):
        return {}
    return {
        item["id"]: item["image"]
        for item in endpoints
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("image"), str)
    }


def user_config(raw: dict[str, Any], chart: dict[str, Any] | None = None) -> dict[str, Any]:
    """Exclude chart fields before computing section fingerprints."""
    owner = raw if chart is None else chart
    normal = {key: value for key, value in raw.items() if key not in chart_owned_keys(owner)}
    endpoints = normal.get("performer_endpoints")
    pins = endpoint_image_pins(owner)
    if isinstance(endpoints, list) and ENDPOINT_IMAGE_KEY in CHART_OWNED_KEYS:
        normal["performer_endpoints"] = [
            {key: value for key, value in item.items() if key != "image"}
            if isinstance(item, dict) and item.get("id") in pins
            else item
            for item in endpoints
        ]
    return normal


def configured_chart_keys() -> set[str]:
    seed = os.environ.get(SEED_ENV)
    if not seed:
        return set()
    try:
        raw = yaml.safe_load(Path(seed).read_text())
    except (OSError, UnicodeError, yaml.YAMLError):
        return set(CHART_OWNED_KEYS)
    return chart_owned_keys(raw) if isinstance(raw, dict) else set(CHART_OWNED_KEYS)


def chart_owned_problem(
    candidate: dict[str, Any], original: dict[str, Any] | None = None,
) -> str | None:
    """Refuse owned edits; unchanged live values may await the next boot overlay."""
    seed = os.environ.get(SEED_ENV)
    if not seed:
        return None
    try:
        raw = yaml.safe_load(Path(seed).read_text())
    except (OSError, UnicodeError, yaml.YAMLError):
        return "Chart seed is unreadable; configuration writes are unavailable."
    if not isinstance(raw, dict):
        return "Chart seed is invalid; configuration writes are unavailable."
    changed = sorted(
        key
        for key in chart_owned_keys(raw)
        if key != ENDPOINT_IMAGE_KEY and candidate.get(key) != raw.get(key)
        and (original is None or candidate.get(key) != original.get(key))
    )
    chart_pins = endpoint_image_pins(raw)
    original_endpoints = (original or {}).get("performer_endpoints")
    original_images = {
        item["id"]: item.get("image")
        for item in (original_endpoints if isinstance(original_endpoints, list) else [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    endpoints = candidate.get("performer_endpoints")
    if isinstance(endpoints, list):
        changed.extend(
            f"performer_endpoints.{item['id']}.image"
            for item in endpoints
            if isinstance(item, dict)
            and item.get("id") in chart_pins
            and item.get("image") != chart_pins[item["id"]]
            and (
                original is None or item["id"] not in original_images
                or item.get("image") != original_images[item["id"]]
            )
        )
    return f"{', '.join(changed)}: {CHART_MANAGED_LABEL}." if changed else None
