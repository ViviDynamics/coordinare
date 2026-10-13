"""Branch-divergence detection for in-flight PR branches (047/096/097, 555).

Coordinare is an orchestrator: it never clones, rebases or pushes a branch.
This module only *decides*. It classifies a branch from the hosting
platform's mergeability verdict, applies the 096 anti-thrash marker, and on
divergence routes the card to the implementer with a relay-feedback
instruction (555). The implementer brings the branch up to date inside its
own container, where git credentials already exist.

The one git command left here is the read-only ``git ls-remote`` in
``fetch_main_sha``.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.models.rebase import RebaseOutcome
from coordinare.services.closed_pr import is_closed_pr_block

if TYPE_CHECKING:
    from collections.abc import Mapping


logger = structlog.get_logger(__name__)

# Branch prefix for coordinare-managed branches.
_COORDINARE_BRANCH_PREFIX = "coordinare/"


_PUBLIC_GITHUB_API_HOSTS = frozenset({"github.com", "api.github.com"})

# Outcomes that mean "nothing moved": do not re-issue against the same
# (main, head). BLOCKED/FAILED are pre-555 values still found in snapshots.
_NON_PROGRESSING_OUTCOMES = frozenset({
    RebaseOutcome.DELEGATED.value, RebaseOutcome.BLOCKED.value, RebaseOutcome.FAILED.value,
})


def repo_url_from_config(config: object) -> str:
    """Build the HTTPS clone URL from coordinare config.

    Uses ``parsed.hostname`` + ``parsed.port`` (not ``.netloc``) so that any
    userinfo (``user:pass@host``) embedded in ``github_api_url`` is never
    propagated into the clone URL or logs.  Non-standard ports are still
    preserved.  Returns ``""`` when org or project is unset.
    """
    from urllib.parse import urlparse

    org = str(getattr(config, "github_org", "") or "")
    project = str(getattr(config, "project_name", "") or "")
    if not org or not project:
        return ""
    # 151: prefer an explicitly-configured git host (the board-sim bench sets
    # git_base_url to a loopback git:// daemon). A *defaulted* git_base_url falls
    # through to the github_api_url-derived host so GitHub Enterprise — where the
    # API host and git host differ — is unaffected (FR-013). pydantic's
    # model_fields_set is what distinguishes set-to-the-default from defaulted,
    # so an operator who explicitly writes the default host still gets this branch.
    git_base = str(getattr(config, "git_base_url", "") or "").rstrip("/")
    if git_base and "git_base_url" in getattr(config, "model_fields_set", ()):
        return f"{git_base}/{org}/{project}.git"
    api_url = str(getattr(config, "github_api_url", "") or "")
    host = "github.com"
    if api_url:
        parsed = urlparse(api_url)
        hostname = (parsed.hostname or "").lower()
        if hostname and hostname not in _PUBLIC_GITHUB_API_HOSTS:
            # Wrap IPv6 literals in brackets so the URL stays valid
            # (urlparse strips them from .hostname, so detect via ":").
            bracketed = f"[{hostname}]" if ":" in hostname else hostname
            host = f"{bracketed}:{parsed.port}" if parsed.port else bracketed
    return f"https://{host}/{org}/{project}.git"


async def _run_git(
    args: list[str],
    cwd: str,
    *,
    env: dict[str, str] | None = None,
    timeout: int = 120,
) -> tuple[int, str, str]:
    """Run a git command via async subprocess.

    Returns ``(exit_code, stdout, stderr)``.
    """
    import os

    full_env = {**os.environ, **(env or {})}
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=full_env,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
        return (
            proc.returncode or 0,
            stdout_bytes.decode(errors="replace").strip(),
            stderr_bytes.decode(errors="replace").strip(),
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 128, "", "git command timed out"
    except Exception as exc:
        return 128, "", str(exc)


def _git_env_for_token(token: str) -> dict[str, str]:
    """Build env vars that inject a GitHub token into git HTTP auth.

    Uses the ``x-access-token`` Basic-auth pattern (same as
    WorkspaceManager) and suppresses git trace vars so tokens don't
    leak into subprocess debug output.
    """
    import base64
    credentials = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credentials}",
        # Disable interactive prompts so git fails fast on auth issues
        "GIT_TERMINAL_PROMPT": "0",
        # Suppress all trace output to avoid token leakage in subprocess stderr
        "GIT_TRACE": "0",
        "GIT_TRACE_PACKET": "0",
        "GIT_TRACE_PERFORMANCE": "0",
        "GIT_TRACE_SETUP": "0",
        "GIT_CURL_VERBOSE": "0",
    }


async def fetch_main_sha(
    repo_url: str,
    token: str,
    *,
    default_branch: str = "main",
) -> str | None:
    """Get the current HEAD SHA of the default branch without cloning.

    Uses ``git ls-remote``.  Returns the SHA string or None on failure.
    """
    tmp = tempfile.mkdtemp(prefix="coordinare-ls-remote-")
    try:
        env = _git_env_for_token(token)
        exit_code, stdout, stderr = await _run_git(
            ["ls-remote", repo_url, f"refs/heads/{default_branch}"],
            cwd=tmp,
            env=env,
            timeout=30,
        )
        if exit_code != 0:
            logger.warning("fetch_main_sha.failed", error=stderr)
            return None
        # Output format: "<sha>\trefs/heads/main"
        parts = stdout.split()
        return parts[0] if parts else None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _board_pauses_rebase(
    card_id: str, session: dict[str, Any], board_snapshot: Mapping[str, Any] | None,
) -> bool:
    """Durable pause and valid fresh board stop columns forbid divergence requests."""
    card = session.get("current_card") or {}
    identifiers = {card_id, str(card.get("id") or ""), str(card.get("content_id") or "")}
    board = board_snapshot or {}
    return bool(session.get("board_paused")) or any(
        isinstance(board.get(column), list) and identifier in board[column]
        for column in ("BACKLOG", "TODO", "DONE", "BLOCKED") for identifier in identifiers if identifier
    )


def detect_stale_branches(
    active_sessions: dict[str, Any],
    main_sha: str,
    *, board_snapshot: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Identify active sessions whose branches are candidates for a divergence check.

    Returns a list of dicts: ``{card_id, branch, pr_number, phase}``.
    Skips sessions where the performer is actively running (FR-006).
    """
    stale: list[dict[str, Any]] = []
    for card_id, session in active_sessions.items():
        if not isinstance(session, dict) or _board_pauses_rebase(card_id, session, board_snapshot):
            continue
        if is_closed_pr_block(session.get("system_error_reason")):
            continue
        branch = session.get("workspace_branch") or ""
        if not branch.startswith(_COORDINARE_BRANCH_PREFIX):
            continue
        # FR-006: skip branches with active performers — include them
        # in the result with skipped=True so the Slack summary and
        # dashboard show "N skipped (active performer)" instead of
        # silently omitting them.
        phase = session.get("phase", "")
        if phase == "monitoring_performer":
            logger.info(
                "rebase.skipped_active_performer",
                card_id=card_id,
                branch=branch,
            )
            stale.append({
                "card_id": card_id,
                "branch": branch,
                "pr_number": 0,
                "phase": phase,
                "skipped": True,
            })
            continue
        card = session.get("current_card") or {}
        pr_url = card.get("pr_url") or ""
        pr_number = 0
        if pr_url and "/" in pr_url:
            import contextlib
            with contextlib.suppress(ValueError, IndexError):
                pr_number = int(pr_url.rstrip("/").rsplit("/", 1)[-1])
        # Only rebase if the card has an open PR
        if not pr_url:
            continue
        stale.append({
            "card_id": card_id,
            "branch": branch,
            "pr_number": pr_number,
            "phase": phase,
        })
    return stale


def should_attempt_rebase(
    session: Mapping[str, Any], current_main_sha: str, head_sha: str,
) -> bool:
    """096 (FR-007), 555 (FR-007): anti-thrash guard for divergence requests.

    Returns ``False`` only when this card's most recent attempt hit a
    NON-progressing outcome (``DELEGATED``, or a pre-555 ``BLOCKED``/``FAILED``)
    against the SAME ``(main_sha, head_sha)`` — so the implementer is not handed
    the same divergence every cycle. Any change to the branch head (the performer
    pushed work) or the target main re-enables the attempt, as does a prior
    progressing outcome (``CLEAN``/``PERFORMER_RESOLVED``/``SKIPPED``) or no prior
    attempt at all.
    """
    prior = session.get("last_rebase_attempt")
    if not isinstance(prior, dict):
        return True
    if prior.get("outcome") not in _NON_PROGRESSING_OUTCOMES:
        return True
    return not (
        prior.get("main_sha") == current_main_sha and prior.get("head_sha") == head_sha
    )


def classify_pre_dispatch(
    mergeable_raw: str,
    merge_state_status: str,
    head_sha: str,
    session: Mapping[str, Any],
    current_main_sha: str,
) -> str:
    """097/555: decide what to do with an in-flight branch at the dispatch point.

    Pure (no I/O). Maps the platform mergeability + spec-096 anti-thrash marker to
    one of:
    - ``"unknown"``       — mergeability not yet computed (UNKNOWN / empty head);
      the caller dispatches normally (555 FR-005, fail open).
    - ``"proceed"``       — branch is current with main; dispatch unchanged.
    - ``"request"``       — branch is CONFLICTING or BEHIND and the implementer has
      not yet been asked about this (main, head): route it to the implementer.
    - ``"blocked_thrash"``— CONFLICTING/BEHIND but already requested against the
      same (main, head): do not ask again and do not dispatch onto the
      conflicting base (097 FR-008, 555 FR-007).
    """
    raw = (mergeable_raw or "").upper()
    mss = (merge_state_status or "").upper()
    if not head_sha or raw in ("", "UNKNOWN"):
        return "unknown"
    if raw == "CONFLICTING" or mss == "BEHIND":
        if should_attempt_rebase(session, current_main_sha, head_sha):
            return "request"
        return "blocked_thrash"
    return "proceed"


async def _live_pr_allows_rebase(session: Any, github: Any, card_id: str) -> bool:
    """Confirm PR lifecycle before a divergence request; an unreadable state defers."""
    card = session.get("current_card") or {}
    pr_id = str(card.get("pr_node_id") or "")
    live_state = ""
    try:
        if pr_id and github is not None:
            context = await github.get_pr_review_context(pr_id)
            if isinstance(context, dict):
                live_state = str(context.get("state") or "").upper()
    except Exception as exc:
        logger.warning("rebase.pr_state_unreadable", card_id=card_id, error_type=type(exc).__name__)
    allowed = live_state == "OPEN"
    if not allowed:
        logger.info("rebase.pr_state_deferred", card_id=card_id, pr_state=live_state or "UNKNOWN")
    return allowed


def implementer_rebase_enabled(config: object) -> bool:
    """555 (FR-008): divergence handling is opt-in; anything unreadable is off."""
    return bool(getattr(getattr(config, "implementer_rebase", None), "enabled", False))


def build_divergence_feedback(
    branch: str, main_sha: str, *, conflicting: bool, default_branch: str = "main",
) -> dict[str, str]:
    """Build the relay_feedback entry that hands a diverged branch to the implementer.

    Asks for a merge of the default branch, not a history rewrite: the performer
    push path never force-pushes over an existing branch (165), so a rebased
    branch could not be published. PRs are squash-merged, so the result on the
    default branch is the same.
    """
    verdict = (
        f"conflicts with `{default_branch}`" if conflicting
        else f"is behind `{default_branch}`"
    )
    return {
        "body": (
            f"**Branch out of date with {default_branch}**: the PR branch `{branch}` {verdict} "
            f"(`{main_sha[:8]}`). Before anything else in this turn, bring it up to date "
            "in your workspace:\n\n"
            f"1. `git fetch origin {default_branch}` (if the fetch is refused, the "
            f"`origin/{default_branch}` ref from this workspace's fresh clone is current enough).\n"
            f"2. `git merge origin/{default_branch}`.\n"
            "3. Resolve every conflict, keeping the intent of both sides, and remove all "
            "conflict markers.\n"
            "4. Run the project's tests and checks, then commit the merge.\n\n"
            "Do not `git rebase` and do not force-push: this branch is already published "
            "and the push path never overwrites it. Then continue with any other work "
            "this turn carries."
        ),
    }


def request_implementer_rebase(
    session: Any,
    *,
    card_id: str,
    branch: str,
    main_sha: str,
    head_sha: str,
    conflicting: bool,
    reason: str,
    prev_main_sha: str | None = None,
) -> None:
    """555: route a diverged card to the implementer; the daemon touches no code.

    Queues the divergence instruction ahead of any feedback already waiting,
    points the session at the implementer stage via the existing dispatch
    pipeline, and stamps the 096 anti-thrash marker so the same
    ``(main_sha, head_sha)`` is never handed out twice (FR-007).

    Only mutates the session dict. If the implementer cannot resolve the
    divergence, the normal monitor_performer error/blocked path escalates.
    """
    entry = build_divergence_feedback(branch, main_sha, conflicting=conflicting)
    queued = [item for item in (session.get("relay_feedback") or []) if item != entry]
    session["relay_feedback"] = [entry, *queued]
    session["performer_stage"] = "implementing"
    session["phase"] = "dispatching"
    session["agent_dispatch"] = {}
    session["agent_dispatch_at"] = None
    session["last_rebase_attempt"] = {
        "main_sha": main_sha,
        "head_sha": head_sha,
        "outcome": RebaseOutcome.DELEGATED.value,
    }
    logger.info(
        "rebase.triggered", reason=reason,
        card_id=card_id, branch=branch,
        prev_main_sha=prev_main_sha, current_main_sha=main_sha,
        outcome=RebaseOutcome.DELEGATED.value,
    )
    logger.info(
        "rebase.performer_dispatched",
        card_id=card_id, branch=branch, conflicting=conflicting,
    )
