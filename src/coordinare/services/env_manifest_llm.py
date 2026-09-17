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

logger = structlog.get_logger(__name__)

# async (messages) -> assistant text.  Caller wires this to the coordinare's LLM.
ChatJson = Callable[[list[dict[str, str]]], Awaitable[str]]

_SYSTEM_PROMPT = (
    "You extract OS-level system package requirements from a project's README. "
    "Return ONLY a JSON object of the form "
    '{"system": [{"name": "<package>", "binary": "<cmd-on-PATH>"}]}. '
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
        items.append(ManifestItem(name=binary, kind="system", source="README.md"))
    return items


async def enrich_from_readme(
    manifest: EnvManifest,
    readme_text: str,
    chat_json: ChatJson | None,
) -> EnvManifest:
    """Augment ``manifest`` with README-derived system packages. Best-effort."""
    if chat_json is None or not (readme_text or "").strip():
        return manifest
    try:
        reply = await chat_json(build_messages(readme_text, manifest))
    except Exception as exc:  # enrichment must never fail the bootstrap
        logger.warning("env_manifest.readme_llm_failed", error=str(exc))
        return manifest
    known = {i.name for i in manifest.items}
    new_items = parse_system_items(reply, known=known)
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
