"""LLM README pass for the env manifest (077).

The deterministic parsers (:mod:`coordinare.services.env_manifest`) cover pinned
runtimes and declared gems/packages, but some required system packages live only
in README prose ("you'll need Chromium for the system tests", "install
PostgreSQL").  This module asks the coordinare's configured LLM to extract those
system packages and merges them into the manifest as ``kind="system"`` items.

It is deliberately pure: it builds the prompt and parses the reply, taking an
injected ``chat_json`` callable, so it has no HTTP/config coupling and is fully
unit-testable.  The pass is best-effort — any failure leaves the deterministic
manifest untouched.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import structlog

from coordinare.models.env_manifest import EnvManifest, ManifestItem
from coordinare.services.conductor_calls import ConductorCallLog, conductor_call

logger = structlog.get_logger(__name__)

# async (messages) -> assistant text.  Caller wires this to the coordinare's LLM.
ChatJson = Callable[[list[dict[str, str]]], Awaitable[str]]


def _calls_of(chat_json: ChatJson | None) -> ConductorCallLog | None:
    """The call log of the backend a bound ``chat_json`` belongs to (131). None for any other callable."""
    calls = getattr(getattr(chat_json, "__self__", None), "calls", None)
    return calls if isinstance(calls, ConductorCallLog) else None

_SYSTEM_PROMPT = (
    "You derive project dependency manifest items from the provided project documentation. "
    "Return ONLY JSON with system items and activation data when toolchain setup is explicit. "
    'Shape: {"system": [{"name":"<dependency>","binary":"<cmd-on-PATH>","check":"<POSIX check>"}], '
    '"activation": [{"name":"<runtime>","version":"<version>","activation_paths":["<cache-relative glob>"],'
    '"activation_env":{"NAME":"<cache-relative value>"},"check":"<POSIX check>","version_check":"<POSIX check>"}]}. '
    "Include ONLY packages/binaries the README EXPLICITLY says must be installed "
    "to build, test, or run the project at the OS level (e.g. a headless browser "
    "like chromium + its driver, a database client, image libraries). "
    "Do NOT include language runtimes, gems, npm packages, or anything you are "
    "guessing at — if the README does not clearly require it, omit it. "
    "If nothing qualifies, return {\"system\": []}."
)


def build_messages(readme_text: str, existing: EnvManifest) -> list[dict[str, str]]:
    have = sorted({i.name for i in existing.items})
    user = (
        "Already known (do NOT repeat these): "
        + (", ".join(have) if have else "(none)")
        + "\n\nREADME:\n"
        + readme_text[:12000]  # cap prompt size; READMEs rarely need more for deps
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def parse_system_items(reply_text: str, *, known: set[str]) -> list[ManifestItem]:
    """Parse the LLM JSON reply into system ManifestItems, skipping known names."""
    text = (reply_text or "").strip()
    if not text:
        return []
    # Tolerate a fenced ```json block.
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") :] if "{" in text else text
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # Last-ditch: find the first {...} span.
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return []
        try:
            data = json.loads(text[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            return []
    raw = data.get("system") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    items: list[ManifestItem] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        # The binary (PATH check target) is what verify asserts; fall back to name.
        binary = str(entry.get("binary") or entry.get("name") or "").strip()
        if not binary or binary in known or binary in seen:
            continue
        seen.add(binary)
        check = str(entry.get("check") or f"command -v {binary} >/dev/null 2>&1")
        items.append(ManifestItem(name=binary, kind="system", source="README.md", check=check))
    return items


async def enrich_from_readme(
    manifest: EnvManifest,
    readme_text: str,
    chat_json: ChatJson | None,
) -> EnvManifest:
    """Augment ``manifest`` with README-derived system packages. Best-effort."""
    if chat_json is None or not (readme_text or "").strip():
        return manifest
    async with conductor_call(_calls_of(chat_json), kind="env_manifest") as call:
        try:
            reply = await chat_json(build_messages(readme_text, manifest))
        except Exception as exc:  # enrichment must never fail the bootstrap
            call.decide("env manifest: model call failed")
            logger.warning("env_manifest.readme_llm_failed", error=str(exc))
            return manifest
        known = {i.name for i in manifest.items}
        new_items = parse_system_items(reply, known=known)
        call.decide(
            f"env manifest: {len(new_items)} system package(s) from README" if new_items
            else "env manifest: none added",
        )
    if not new_items:
        return manifest
    logger.info(
        "env_manifest.readme_llm_items",
        symphony=manifest.symphony_name,
        added=[i.name for i in new_items],
    )
    return manifest.model_copy(
        update={"items": [*manifest.items, *new_items], "llm_derived": True},
    )
