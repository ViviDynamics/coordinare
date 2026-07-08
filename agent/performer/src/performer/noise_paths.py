"""131: the single source of truth for "noise" paths that must never be
committed into a symphony repo.

Two layers consume this (see spec 131):

* **Prevention** — the workspace writes ``exclude_globs()`` into a clone's
  ``.git/info/exclude`` so a broad ``git add .`` / ``git add -A`` skips these
  directories without touching the target repo's tracked ``.gitignore``.
* **Guard** — the commit/push boundary strips any tracked path matched by
  :func:`path_has_agent_config` (catches ``git add -f`` and already-tracked junk).

The coordinare-side review-diff filter (``_DIFF_NOISE_PATH_MARKERS`` in
``src/coordinare/graph/nodes/dispatch_performer.py``) mirrors ``AGENT_CONFIG_DIRS``;
``tests/unit/test_diff_noise_drift.py`` fails CI if the two ever diverge.

Adding a new backend's config directory here updates prevention, the guard, and
(via the drift test) the diff filter — all at once.
"""
from __future__ import annotations

# Per-backend tool config/state directories that agents create and must never
# commit. Names only (no leading slash / trailing slash) — matched by path
# SEGMENT equality, never substring, so ``.github`` / ``docs/claude-guide`` are
# safe.
AGENT_CONFIG_DIRS: tuple[str, ...] = (
    ".codex",      # codex
    ".claude",     # claude_code
    ".hermes",     # hermes
    ".junie",      # junie
    ".opencode",   # opencode / opencode_compat
    ".openclaw",   # openclaw
    ".pi",         # pi
)

# Build / VCS detritus that is also never a real change. Kept here so the one
# canonical set covers everything the diff filter historically listed.
BUILD_VCS_NOISE: tuple[str, ...] = (
    "node_modules",
    "vendor/bundle",
    ".venv",
    "__pycache__",
    ".tmp",
    ".git",
)


def exclude_globs() -> tuple[str, ...]:
    """Gitignore-style lines for a clone's ``.git/info/exclude``.

    For each agent config dir we emit an anchored form (``/.codex/`` — repo root)
    and a nested form (``**/.codex/``) so the directory is ignored wherever it
    appears, while a merely dot-prefixed *file* or an unrelated path is not.
    """
    lines: list[str] = []
    for name in AGENT_CONFIG_DIRS:
        lines.append(f"/{name}/")
        lines.append(f"**/{name}/")
    return tuple(lines)


def path_has_agent_config(path: str) -> bool:
    """True iff *path* has a segment equal to a known agent-config dir name.

    Segment-equality (not substring) so ``.github/x``, ``.gitignore``,
    ``.env.example`` and ``docs/claude-guide/x`` are NOT matched, but
    ``.codex/session.json``, ``a/b/.claude/state`` and a bare ``.pi`` are.
    """
    if not path:
        return False
    segments = path.replace("\\", "/").split("/")
    return any(seg in AGENT_CONFIG_DIRS for seg in segments)
