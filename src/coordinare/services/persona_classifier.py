"""Persona scope classifier service (spec 074).

Renders the deterministic classifier input (per ``contracts/classifier-prompt.md``),
calls the configured ``ConductingBackend`` with a wall-clock budget, parses the
JSON response, runs post-processing (closer invariant, drop unknown personas,
fill missing personas), and returns a ``PersonaScope`` for ``CardSession``.

The coordinare never sees the raw diff body — only paths, ±LOC, file status, and
path-class memberships derived from operator-owned globs (FR-002).

Returns ``None`` on any failure or when prerequisites aren't met; callers
treat ``None`` as "no classification this cycle" and proceed with full-depth
defaults (FR-006 fallback lives in the node + dispatch layer; this service
keeps a narrow contract).
"""
from __future__ import annotations

import asyncio
import fnmatch
import json
import re
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import structlog

if TYPE_CHECKING:
    from pathlib import Path

    from coordinare.session import PersonaScope, PersonaScopeSlice

logger = structlog.get_logger(__name__)


# Personas the classifier addresses (closer is included so the focus string is
# advisory to the closer, but closer's depth is scope-invariant — always full).
CLASSIFIER_PERSONAS: tuple[str, ...] = ("reviewer", "security", "qa", "tech_writer", "closer")

VALID_DEPTHS: frozenset[str] = frozenset(("skim", "normal", "full", "skip"))

DEPTH_DEFINITIONS: dict[str, str] = {
    "skim": "Spot-check; look for obvious correctness or risk issues only. ~5 tool calls.",
    "normal": "Standard review for this persona. ~15 tool calls.",
    "full": "Thorough investigation; check invariants, edge cases, integration points. ~30+ tool calls.",
    "skip": "Do not run this persona. Use only when the change has no surface area for this persona.",
}

# Verbatim from contracts/classifier-prompt.md — keep in sync.
SYSTEM_PROMPT = """You are coordinare's persona scope classifier. For a single pull request, decide
how much attention each downstream persona (reviewer, security, qa, tech_writer,
closer) should pay to the change.

You will receive:
  - the card's title and summary
  - a per-file summary of the diff (path, ±LOC, status, path-class memberships)
  - the project's path-class taxonomy (which globs count as docs, config, tests,
    runtime, security_sensitive, etc.)
  - excerpts from the project's CLAUDE.md and AGENTS.md to understand conventions

You will NEVER see the raw diff body. Reason from paths, sizes, and project
context only.

For each persona, output:
  depth: one of "skim", "normal", "full", "skip"
  focus: one to three sentences of plain prose — what should this persona pay
         attention to on this specific change? Be concrete. Reference file
         paths or behaviors by name when helpful. Do not write bullet lists.

Guidelines:
  - Default to "normal" when uncertain. Use "skim" only when the persona's
    domain is genuinely orthogonal to the change (e.g., security on a docs-only
    PR). Use "full" when the change touches the persona's core concern (e.g.,
    security on an auth file). Use "skip" sparingly — only when the change has
    zero surface area for that persona.
  - The "closer" persona's depth field will be ignored — closer always runs.
    Still emit a depth for it (any value is fine); the focus field IS read by
    closer as advisory context, so write it carefully.
  - Path-class membership is a strong signal but not absolute. A 5-line change
    in a security-sensitive file may still warrant "full" for security and
    "skim" for tech_writer.
  - Test-only changes (only `tests` class touched) typically warrant skim for
    reviewer, skip for security/qa, and skip for tech_writer.
  - Documentation-only changes (only `docs` class touched) typically warrant
    skim for reviewer, skip for security/qa, and full for tech_writer.

Respond with a single JSON object matching the output schema. Do not include
any prose outside the JSON."""

PROJECT_CONTEXT_BYTES = 4096


# Module-level cooldown for `persona_scope.classifier.failed` warnings (FR-006).
# Keyed by reason so different failure modes don't suppress each other.  Test
# code can call ``reset_failure_warning_cooldown()`` between cases.
_LAST_WARN_AT: dict[str, float] = {}


def reset_failure_warning_cooldown() -> None:
    """Test hook: clear the rate-limit window so consecutive runs warn fresh."""
    _LAST_WARN_AT.clear()


def _warn_failure(
    *,
    card_id: str,
    cycle_index: int,
    reason: str,
    cooldown_seconds: float,
) -> None:
    """Emit ``persona_scope.classifier.failed`` warning, rate-limited per reason."""
    now = time.monotonic()
    last = _LAST_WARN_AT.get(reason)
    if last is not None and (now - last) < cooldown_seconds:
        logger.debug(
            "persona_scope.classifier.warning_suppressed",
            card_id=card_id,
            cycle_index=cycle_index,
            reason=reason,
            cooldown_seconds=cooldown_seconds,
        )
        return
    _LAST_WARN_AT[reason] = now
    logger.warning(
        "persona_scope.classifier.failed",
        card_id=card_id,
        cycle_index=cycle_index,
        reason=reason,
        fallback_to_full=True,
    )


def _build_full_fallback_scope(
    *,
    reason: str,
    cycle_index: int,
    classifier_model: str,
    head_sha: str,
    files_summary: list[dict[str, Any]],
) -> PersonaScope:
    """FR-006: full-everywhere fallback used when no prior scope is available."""
    personas: dict[str, PersonaScopeSlice] = {}
    for name in CLASSIFIER_PERSONAS:
        personas[name] = {
            "depth": "full",
            "focus": "(classifier unavailable — full depth applied)",
            "overrides": [f"classifier_failed:{reason}"],
        }
    _apply_closer_invariant(personas)
    scope: PersonaScope = {
        "computed_at": datetime.now(UTC).isoformat(),
        "cycle_index": cycle_index,
        "classifier_model": classifier_model or "",
        "head_sha": head_sha,
        "files_summary": files_summary,
        "personas": personas,
    }
    return scope


def _fallback_scope(
    *,
    session: dict[str, Any],
    reason: str,
    card_id: str,
    cycle_index: int,
    classifier_model: str,
    head_sha: str,
    files_summary: list[dict[str, Any]],
    cooldown_seconds: float,
) -> PersonaScope:
    """Emit warning + return prior-cycle scope if available, else full-fallback."""
    _warn_failure(
        card_id=card_id,
        cycle_index=cycle_index,
        reason=reason,
        cooldown_seconds=cooldown_seconds,
    )
    prior = session.get("persona_scope")
    if isinstance(prior, dict) and prior.get("personas"):
        logger.debug(
            "persona_scope.classifier.reusing_previous_cycle",
            card_id=card_id,
            cycle_index=cycle_index,
            reason=reason,
            prior_cycle_index=prior.get("cycle_index"),
        )
        # Refresh cycle bookkeeping so snapshots don't carry permanently-stale
        # cycle_index/computed_at after repeated reuse.  Persona slices and
        # files_summary are intentionally preserved as the carry-forward signal.
        refreshed = dict(prior)
        refreshed["cycle_index"] = cycle_index
        refreshed["computed_at"] = datetime.now(UTC).isoformat()
        # head_sha is intentionally carried forward from the prior scope: it
        # reflects the head that was last *successfully classified*, not the
        # current cycle's head.  Do not use it as a cache key for the current
        # head — it is metadata about provenance.
        return refreshed  # type: ignore[return-value]
    return _build_full_fallback_scope(
        reason=reason,
        cycle_index=cycle_index,
        classifier_model=classifier_model,
        head_sha=head_sha,
        files_summary=files_summary,
    )


def _match_path_classes(file_path: str, path_classes: dict[str, list[str]]) -> list[str]:
    """Return the list of path-class names whose globs match ``file_path``.

    Pure function: no I/O, deterministic order (preserves ``path_classes``
    insertion order).  Globs use ``fnmatch`` semantics — '**' is treated by
    ``fnmatch`` as '*', so callers should rely on the POSIX-shell glob
    interpretation of ``config-schema.md`` (operator-facing patterns) rather
    than expecting Python-pathlib ``**`` recursion.

    Empty / malformed patterns are skipped silently (validation belongs in
    PersonaScopeConfig._validate_path_classes at startup).
    """
    if not file_path or not path_classes:
        return []
    hits: list[str] = []
    for class_name, globs in path_classes.items():
        if not isinstance(globs, (list, tuple)):
            continue
        for pattern in globs:
            if not isinstance(pattern, str) or not pattern:
                continue
            if fnmatch.fnmatch(file_path, pattern):
                hits.append(class_name)
                break
    return hits


def _read_project_context(workspace_path: Path | None) -> dict[str, str]:
    """Read first ``PROJECT_CONTEXT_BYTES`` of CLAUDE.md and AGENTS.md (if present)."""
    out = {"claude_md_head": "", "agents_md_head": ""}
    if workspace_path is None:
        return out
    try:
        claude = workspace_path / "CLAUDE.md"
        if claude.is_file():
            out["claude_md_head"] = claude.read_text(encoding="utf-8", errors="replace")[:PROJECT_CONTEXT_BYTES]
        agents = workspace_path / "AGENTS.md"
        if agents.is_file():
            out["agents_md_head"] = agents.read_text(encoding="utf-8", errors="replace")[:PROJECT_CONTEXT_BYTES]
    except Exception as exc:
        logger.debug("persona_scope.classifier.project_context_read_failed", error=str(exc))
    return out


def _build_files_summary(
    pr_files: list[dict[str, Any]],
    path_classes: dict[str, list[str]],
) -> list[dict[str, Any]]:
    """Annotate raw PR files with path-class memberships (per FR-002 wire format)."""
    out: list[dict[str, Any]] = []
    for f in pr_files:
        path = str(f.get("path") or "")
        if not path:
            continue
        out.append(
            {
                "path": path,
                "added": int(f.get("added") or 0),
                "removed": int(f.get("removed") or 0),
                "status": str(f.get("status") or "modified"),
                "classes": _match_path_classes(path, path_classes),
            },
        )
    return out


def _render_input(
    *,
    card: dict[str, Any],
    pr_number: int,
    head_sha: str,
    files_summary: list[dict[str, Any]],
    project_context: dict[str, str],
    personas: list[str],
    path_classes: dict[str, list[str]],
) -> dict[str, Any]:
    return {
        "card": {
            "id": str(card.get("id") or ""),
            "title": str(card.get("title") or ""),
            "summary": str(card.get("body") or card.get("summary") or "")[:4096],
        },
        "pr": {
            "number": pr_number,
            "head_sha": head_sha,
            "files": files_summary,
        },
        "project_context": project_context,
        "personas": personas,
        "path_classes": path_classes,
        "depth_definitions": DEPTH_DEFINITIONS,
    }


def _render_user_message(input_json: dict[str, Any]) -> str:
    payload = json.dumps(input_json, indent=2, sort_keys=False)
    return (
        "Classify the following pull request:\n\n"
        "<input>\n"
        f"{payload}\n"
        "</input>\n\n"
        "Output the JSON object now."
    )


def _apply_forced_full(
    personas: dict[str, PersonaScopeSlice],
    files_summary: list[dict[str, Any]],
    forced_full_on_path_classes: dict[str, list[str]],
    card_id: str,
) -> None:
    """FR-005 / US2: override a persona's depth to ``full`` when the change touches
    any path-class the operator has marked as forcing-full for that persona.

    Appends ``forced_full_on_path_class:<class>`` to the persona's ``overrides`` and
    emits ``persona_scope.classifier.forced_full`` per (persona, class) hit.
    Surgical: does not affect personas not configured to force-full.
    """
    if not forced_full_on_path_classes or not files_summary:
        return
    for persona_name, class_list in forced_full_on_path_classes.items():
        if persona_name not in personas or not class_list:
            continue
        for class_name in class_list:
            matching = [
                f["path"] for f in files_summary
                if class_name in (f.get("classes") or [])
            ]
            if not matching:
                continue
            slice_ = personas[persona_name]
            slice_["depth"] = "full"
            overrides = slice_.get("overrides") or []
            tag = f"forced_full_on_path_class:{class_name}"
            if tag not in overrides:
                overrides.append(tag)
            slice_["overrides"] = overrides
            logger.info(
                "persona_scope.classifier.forced_full",
                card_id=card_id,
                persona=persona_name,
                path_class=class_name,
                matching_files=matching,
            )


def _apply_closer_invariant(personas: dict[str, PersonaScopeSlice]) -> None:
    """FR-009: closer's depth is scope-invariant — always full + tagged override."""
    if "closer" in personas:
        slice_ = personas["closer"]
        slice_["depth"] = "full"
        overrides = slice_.get("overrides") or []
        if "closer_is_scope_invariant" not in overrides:
            overrides.append("closer_is_scope_invariant")
        slice_["overrides"] = overrides


def _drop_unknown_personas(
    output_personas: dict[str, Any],
    expected: list[str],
    card_id: str,
) -> dict[str, Any]:
    """Step 5: drop persona keys the coordinare didn't ask for (debug log per FR-015)."""
    expected_set = set(expected)
    cleaned: dict[str, Any] = {}
    for name, payload in output_personas.items():
        if name in expected_set:
            cleaned[name] = payload
        else:
            logger.debug(
                "persona_scope.classifier.unknown_persona_in_output",
                card_id=card_id,
                unknown_persona=name,
            )
    return cleaned


def _fill_missing_personas(
    output_personas: dict[str, PersonaScopeSlice],
    expected: list[str],
) -> dict[str, PersonaScopeSlice]:
    """Step 6: fill any missing persona with depth=full + ``missing_from_classifier_output`` tag."""
    for name in expected:
        if name not in output_personas:
            output_personas[name] = {
                "depth": "full",
                "focus": "(no classifier output — defaulting to full)",
                "overrides": ["missing_from_classifier_output"],
            }
    return output_personas


def _validate_and_coerce_slice(raw: Any) -> PersonaScopeSlice | None:
    if not isinstance(raw, dict):
        return None
    depth = raw.get("depth")
    if not isinstance(depth, str) or depth not in VALID_DEPTHS:
        return None
    focus = raw.get("focus")
    if not isinstance(focus, str):
        focus = ""
    slice_: PersonaScopeSlice = {
        "depth": depth,  # type: ignore[typeddict-item]
        "focus": focus.strip(),
        "overrides": [],
    }
    return slice_


async def classify(
    *,
    session: dict[str, Any],
    card: dict[str, Any],
    conducting_backend: Any,
    config: Any,
    github_service: Any = None,
    workspace_path: Path | None = None,
    classifier_model: str = "",
) -> PersonaScope | None:
    """Classify per-persona scope for the active card.

    Returns ``None`` on any soft failure (no PR, no github_service, parse
    failure, timeout, validation failure).  The node treats ``None`` as a
    no-classification cycle; FR-006 fallback (full-depth-everywhere) is the
    natural consequence of the absent ``persona_scope`` on the session.

    Parameters
    ----------
    session:
        Active ``CardSession`` (used for ``feedback_cycle_count`` as cycle_index).
    card:
        The active card dict (provides ``id``, ``title``, ``body``, ``pr_url``).
    conducting_backend:
        ``ConductingBackend`` implementation (must expose async ``prompt``).
    config:
        Project ``ProjectConfiguration`` (reads ``persona_scope`` block).
    github_service:
        Required for PR-files fetch.  ``None`` ⇒ return ``None``.
    workspace_path:
        Optional workspace dir for reading CLAUDE.md / AGENTS.md heads.
    classifier_model:
        Display name for logging + scope metadata (typically ``config.conducting.model``).

    Notes
    -----
    The ``conducting_backend.prompt`` call is invoked with
    ``response_format="json"``; backends that do not honour this contract (or
    that return non-JSON text wrapped in prose) will fail json.loads here and
    fall through to FR-006 fallback. Any backend wired to this classifier
    must guarantee a JSON-only response.
    """
    persona_scope_cfg = getattr(config, "persona_scope", None)
    if persona_scope_cfg is None or not getattr(persona_scope_cfg, "enabled", False):
        return None
    path_classes: dict[str, list[str]] = dict(getattr(persona_scope_cfg, "path_classes", {}) or {})
    if not path_classes:
        return None

    card_id = str(card.get("id") or "")
    pr_url = card.get("pr_url")
    if not pr_url or not github_service:
        return None

    # Parse owner/repo/pr_number from pr_url. Anchored to ``/<owner>/<repo>/pull/<n>``
    # so query strings, fragments, trailing slashes, and non-GitHub hosts (GHE) are
    # tolerated, and malformed URLs degrade gracefully to ``None``.
    try:
        parsed = urlparse(str(pr_url))
        match = re.search(r"/([^/]+)/([^/]+)/pull/(\d+)(?:/|$)", parsed.path)
        if not match:
            return None
        owner, repo, pr_number_str = match.group(1), match.group(2), match.group(3)
        pr_number = int(pr_number_str)
    except (ValueError, AttributeError):
        return None

    try:
        pr_data = await github_service.get_pr_files(owner, repo, pr_number)
    except Exception as exc:
        logger.warning("persona_scope.classifier.pr_files_failed", card_id=card_id, error=str(exc))
        pr_data = {"files": [], "head_sha": "", "truncated": False, "error": f"raised:{type(exc).__name__}"}

    pr_files = pr_data.get("files") or []
    head_sha = str(pr_data.get("head_sha") or "")
    pr_fetch_error = pr_data.get("error")
    pr_truncated = bool(pr_data.get("truncated"))

    cycle_index = int(session.get("feedback_cycle_count") or 0)
    warn_cooldown = float(
        getattr(persona_scope_cfg, "classifier_failure_warning_cooldown_seconds", 600.0),
    )

    if pr_fetch_error:
        logger.warning(
            "persona_scope.classifier.gh_outage",
            card_id=card_id,
            error=str(pr_fetch_error),
        )
        return _fallback_scope(
            session=session,
            reason=f"gh_outage:{pr_fetch_error}",
            card_id=card_id,
            cycle_index=cycle_index,
            classifier_model=classifier_model,
            head_sha=head_sha,
            files_summary=[],
            cooldown_seconds=warn_cooldown,
        )

    if not pr_files:
        return None

    files_summary = _build_files_summary(pr_files, path_classes)
    if pr_truncated:
        logger.info(
            "persona_scope.classifier.files_truncated",
            card_id=card_id,
            file_count=len(files_summary),
        )
    project_context = _read_project_context(workspace_path)
    expected_personas = list(CLASSIFIER_PERSONAS)

    input_json = _render_input(
        card=card,
        pr_number=pr_number,
        head_sha=head_sha,
        files_summary=files_summary,
        project_context=project_context,
        personas=expected_personas,
        path_classes=path_classes,
    )
    prompt_body = SYSTEM_PROMPT + "\n\n" + _render_user_message(input_json)

    budget = float(getattr(persona_scope_cfg, "classifier_latency_budget_seconds", 30.0))

    def _fail(reason: str) -> PersonaScope:
        return _fallback_scope(
            session=session,
            reason=reason,
            card_id=card_id,
            cycle_index=cycle_index,
            classifier_model=classifier_model,
            head_sha=head_sha,
            files_summary=files_summary,
            cooldown_seconds=warn_cooldown,
        )

    logger.debug(
        "persona_scope.classifier.start",
        card_id=card_id,
        cycle_index=cycle_index,
        head_sha=head_sha,
        file_count=len(files_summary),
        persona_count=len(expected_personas),
        model=classifier_model,
    )
    t0 = time.monotonic()
    try:
        result = await asyncio.wait_for(
            conducting_backend.prompt(prompt_body, response_format="json"),
            timeout=budget,
        )
    except TimeoutError:
        return _fail("timeout")
    except Exception as exc:
        return _fail(f"backend_error:{type(exc).__name__}")
    latency_ms = int((time.monotonic() - t0) * 1000)

    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return _fail("parse_failed")

    raw_personas = data.get("personas")
    if not isinstance(raw_personas, dict):
        return _fail("schema_invalid")

    # Validate + coerce each slice.
    coerced: dict[str, PersonaScopeSlice] = {}
    for name, raw in raw_personas.items():
        slice_ = _validate_and_coerce_slice(raw)
        if slice_ is not None:
            coerced[name] = slice_

    if not coerced:
        # Every slice rejected (e.g. unknown depth values across the board).
        return _fail("all_slices_invalid")

    cleaned = _drop_unknown_personas(coerced, expected_personas, card_id)
    filled = _fill_missing_personas(cleaned, expected_personas)
    forced_full_cfg: dict[str, list[str]] = dict(
        getattr(persona_scope_cfg, "forced_full_on_path_classes", {}) or {},
    )
    _apply_forced_full(filled, files_summary, forced_full_cfg, card_id)
    if pr_truncated:
        # Beyond 300 files the classifier saw only a prefix — lean to full so we
        # don't ship a skim decision based on a partial view of the PR.
        for name in expected_personas:
            if name in filled and filled[name]["depth"] != "skip":
                if filled[name]["depth"] != "full":
                    filled[name]["overrides"].append("pr_files_truncated")
                filled[name]["depth"] = "full"
    _apply_closer_invariant(filled)

    scope: PersonaScope = {
        "computed_at": datetime.now(UTC).isoformat(),
        "cycle_index": cycle_index,
        "classifier_model": classifier_model or "",
        "head_sha": head_sha,
        "files_summary": files_summary,
        "personas": filled,
    }
    logger.info(
        "persona_scope.classifier.complete",
        card_id=card_id,
        cycle_index=cycle_index,
        latency_ms=latency_ms,
        depths={name: filled[name]["depth"] for name in expected_personas if name in filled},
    )
    return scope
