"""Auto-rebase service for in-flight PR branches (047).

After a PR merges to main, rebases every other coordinare-managed branch
onto the new main HEAD.  Clean rebases are force-pushed with lease.
Conflicted rebases return conflict info for performer-driven resolution.

All git operations use ephemeral temp clones (same pattern as
WorkspaceManager) and the async subprocess wrapper ``_run_git``.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from typing import Any

import structlog

from coordinare.models.rebase import RebaseJob, RebaseOutcome, RebaseRound

logger = structlog.get_logger(__name__)

# Branch prefix for coordinare-managed branches.
_COORDINARE_BRANCH_PREFIX = "coordinare/"


_PUBLIC_GITHUB_API_HOSTS = frozenset({"github.com", "api.github.com"})


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


def detect_stale_branches(
    active_sessions: dict[str, Any],
    main_sha: str,
) -> list[dict[str, Any]]:
    """Identify active sessions whose branches may need rebasing.

    Returns a list of dicts: ``{card_id, branch, pr_number, phase}``.
    Skips sessions where the performer is actively running (FR-006).
    """
    stale: list[dict[str, Any]] = []
    for card_id, session in active_sessions.items():
        if not isinstance(session, dict):
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
    session: dict[str, Any], current_main_sha: str, head_sha: str
) -> bool:
    """096 (FR-007): anti-thrash guard for the proactive conflicting-branch rebase.

    Returns ``False`` only when this card's most recent rebase attempt hit a
    NON-progressing outcome (``BLOCKED``/``FAILED``) against the SAME
    ``(main_sha, head_sha)`` — so an unresolvable conflict is not re-attempted
    every cycle. Any change to the branch head (the performer pushed work) or the
    target main re-enables the attempt, as does a prior progressing outcome
    (``CLEAN``/``PERFORMER_RESOLVED``/``SKIPPED``) or no prior attempt at all.
    """
    prior = session.get("last_rebase_attempt")
    if not isinstance(prior, dict):
        return True
    if prior.get("outcome") not in (RebaseOutcome.BLOCKED.value, RebaseOutcome.FAILED.value):
        return True
    return not (
        prior.get("main_sha") == current_main_sha and prior.get("head_sha") == head_sha
    )


async def rebase_branch(
    repo_url: str,
    branch: str,
    main_sha: str,
    token: str,
    *,
    timeout: int = 120,
    default_branch: str = "main",
) -> RebaseJob:
    """Rebase a single branch onto the current main HEAD.

    Clones the repo into a temp directory, fetches main + the branch,
    runs ``git rebase origin/{default_branch}``, and returns a RebaseJob
    with the outcome.

    Does NOT force-push — the caller handles that after checking the
    outcome.
    """
    job = RebaseJob(card_id="", branch=branch, target_main_sha=main_sha)
    start = time.monotonic()
    tmp = tempfile.mkdtemp(prefix="coordinare-rebase-")
    env = _git_env_for_token(token)
    try:
        # Partial clone (blob filter) — fetches commits + trees but defers
        # blob downloads until checkout.  Faster than a full clone while
        # preserving enough history for rebase + merge-base detection.
        exit_code, _, stderr = await _run_git(
            ["clone", "--no-checkout", "--filter=blob:none", repo_url, "repo"],
            cwd=tmp, env=env, timeout=timeout,
        )
        if exit_code != 0:
            job.outcome = RebaseOutcome.FAILED
            job.conflict_preview = f"Clone failed: {stderr[:300]}"
            return job

        repo_dir = f"{tmp}/repo"

        # Fetch the branch
        exit_code, _, stderr = await _run_git(
            ["fetch", "origin", branch], cwd=repo_dir, env=env, timeout=60,
        )
        if exit_code != 0:
            job.outcome = RebaseOutcome.FAILED
            job.conflict_preview = f"Fetch branch failed: {stderr[:300]}"
            return job

        # Checkout the branch
        exit_code, _, stderr = await _run_git(
            ["checkout", "-B", branch, f"origin/{branch}"],
            cwd=repo_dir, env=env, timeout=30,
        )
        if exit_code != 0:
            job.outcome = RebaseOutcome.FAILED
            job.conflict_preview = f"Checkout failed: {stderr[:300]}"
            return job

        # Record pre-rebase SHA
        _, sha_out, _ = await _run_git(["rev-parse", "HEAD"], cwd=repo_dir, timeout=10)
        job.pre_rebase_sha = sha_out.strip()

        # Check if already up-to-date (merge-base == main_sha)
        _, merge_base, _ = await _run_git(
            ["merge-base", "HEAD", f"origin/{default_branch}"],
            cwd=repo_dir, timeout=10,
        )
        if merge_base.strip() == main_sha:
            job.outcome = RebaseOutcome.SKIPPED
            job.post_rebase_sha = job.pre_rebase_sha
            return job

        # Rebase onto main
        exit_code, _rebase_stdout, stderr = await _run_git(
            ["rebase", f"origin/{default_branch}"],
            cwd=repo_dir, env=env, timeout=timeout,
        )
        if exit_code == 0:
            # Clean rebase
            _, new_sha, _ = await _run_git(["rev-parse", "HEAD"], cwd=repo_dir, timeout=10)
            job.post_rebase_sha = new_sha.strip()
            job.outcome = RebaseOutcome.CLEAN
            # Store repo_dir path so caller can force-push from it
            job.repo_dir = repo_dir
            job.tmp_dir = tmp
            return job

        # Non-zero exit — check if it's a real conflict or some other failure
        conflicted_files, conflict_preview = await extract_conflict_info(repo_dir)
        if conflicted_files:
            # Real conflict with identifiable files
            job.conflicted_files = conflicted_files
            job.conflict_preview = conflict_preview
            job.outcome = RebaseOutcome.BLOCKED
        else:
            # Non-conflict failure (missing ref, interrupted rebase, etc.)
            job.outcome = RebaseOutcome.FAILED
            job.conflict_preview = f"Rebase failed (not a merge conflict): {stderr[:300]}"
        # Abort the rebase so the working tree is clean for cleanup
        await _run_git(["rebase", "--abort"], cwd=repo_dir, timeout=10)
        return job

    except Exception as exc:
        job.outcome = RebaseOutcome.FAILED
        job.conflict_preview = f"Unexpected error: {exc}"
        return job
    finally:
        job.duration_seconds = round(time.monotonic() - start, 2)
        # Only clean up if we didn't return a CLEAN result (caller needs the repo dir)
        if job.outcome != RebaseOutcome.CLEAN:
            shutil.rmtree(tmp, ignore_errors=True)


async def extract_conflict_info(repo_dir: str) -> tuple[list[str], str]:
    """Extract conflict details from a failed rebase working tree.

    Returns ``(conflicted_files, conflict_preview)`` where
    ``conflict_preview`` is a truncated string of conflict markers.
    """
    _, diff_out, _ = await _run_git(
        ["diff", "--name-only", "--diff-filter=U"],
        cwd=repo_dir, timeout=10,
    )
    conflicted_files = [f for f in diff_out.splitlines() if f.strip()]

    preview_parts: list[str] = []
    for fpath in conflicted_files[:5]:
        try:
            import os
            full = os.path.join(repo_dir, fpath)
            with open(full, errors="replace") as f:
                content = f.read(2000)
            # Extract just the conflict sections
            in_conflict = False
            conflict_lines: list[str] = []
            for line in content.splitlines():
                if line.startswith("<<<<<<<"):
                    in_conflict = True
                if in_conflict:
                    conflict_lines.append(line)
                if line.startswith(">>>>>>>"):
                    in_conflict = False
                    break
            if conflict_lines:
                preview_parts.append(f"--- {fpath} ---\n" + "\n".join(conflict_lines[:20]))
        except Exception:
            preview_parts.append(f"--- {fpath} --- (could not read)")

    return conflicted_files, "\n\n".join(preview_parts)[:1500]


async def force_push_with_lease(
    repo_dir: str,
    branch: str,
    expected_sha: str,
    token: str,
) -> bool:
    """Force-push with lease.  Returns True on success, False on failure.

    Single attempt — on lease rejection (concurrent push landed between
    our rebase and this push) the caller lets the next rebase round
    re-clone and re-rebase from the updated remote state.
    """
    env = _git_env_for_token(token)
    # Single attempt — if the lease check fails (concurrent push landed
    # between our rebase and this push), we fail and let the next rebase
    # round re-clone + re-rebase from the updated remote state.  Retrying
    # with an updated lease but without re-rebasing would overwrite the
    # intervening commits.
    exit_code, _, stderr = await _run_git(
        ["push", f"--force-with-lease={branch}:{expected_sha}",
         "origin", f"HEAD:{branch}"],
        cwd=repo_dir, env=env, timeout=60,
    )
    if exit_code == 0:
        return True
    logger.warning(
        "rebase.push_failed",
        branch=branch,
        stderr=stderr[:200],
    )
    return False


def build_conflict_feedback(job: RebaseJob) -> list[dict[str, str]]:
    """Build relay_feedback entries for a conflicted rebase.

    Returns a list of dicts that can be passed to the implementer via
    ``state["relay_feedback"]`` so the existing dispatch_performer →
    monitor_performer pipeline handles the resolution.
    """
    feedback: list[dict[str, str]] = []
    if job.conflicted_files:
        files_list = ", ".join(job.conflicted_files[:10])
        feedback.append({
            "body": (
                f"**Rebase conflict** — the branch `{job.branch}` could not be "
                f"cleanly rebased onto main (`{job.target_main_sha[:8]}`). "
                f"Conflicted files: {files_list}\n\n"
                "Please resolve the merge conflicts in these files, then commit "
                "the resolution. The coordinare will force-push the resolved branch."
            ),
        })
    if job.conflict_preview:
        feedback.append({
            "body": f"**Conflict preview:**\n```\n{job.conflict_preview[:800]}\n```",
        })
    return feedback


def prepare_conflict_resolution(
    job: RebaseJob,
    state: dict[str, Any],
    human_reviewers: list[str] | None = None,
) -> None:
    """Set up a session's state for performer-driven conflict resolution.

    Populates ``relay_feedback`` with conflict details and routes the card
    back to the implementer stage via the existing dispatch pipeline.

    Note: this function only mutates the session dict — it does NOT post
    a GitHub comment or block the card.  If the performer fails to resolve
    the conflict, the normal monitor_performer error/blocked path handles
    escalation.
    """
    feedback = build_conflict_feedback(job)
    if not feedback:
        return

    state["relay_feedback"] = feedback  # type: ignore[typeddict-unknown-key]
    state["performer_stage"] = "implementing"
    state["phase"] = "dispatching"
    state["agent_dispatch"] = {}
    state["agent_dispatch_at"] = None

    logger.info(
        "rebase.performer_dispatched",
        card_id=job.card_id,
        branch=job.branch,
        conflicted_files=job.conflicted_files,
    )


def build_conflict_block_comment(
    job: RebaseJob,
    human_reviewers: list[str] | None = None,
    header: str = "",
) -> str:
    """Build a diagnostic GitHub issue comment for an unresolvable conflict.

    ``header`` is an optional standardized attribution header (spec 077). A
    rebase conflict concerns the implementer's branch, so callers with config
    access should pass ``coordinare_attribution(config, "implementing")``; it is
    prepended above the visible comment body.
    """
    mentions = ""
    if human_reviewers:
        mentions = " ".join(f"@{r}" for r in human_reviewers) + " "

    files = "\n".join(f"- `{f}`" for f in job.conflicted_files[:10])
    preview = job.conflict_preview[:800] if job.conflict_preview else "(no preview available)"

    prefix = f"{header}\n\n" if header else ""
    return (
        f"{prefix}{mentions}**Rebase conflict on `{job.branch}`**\n\n"
        f"Could not rebase onto main (`{job.target_main_sha[:8]}`). "
        f"The performer was unable to resolve the conflicts automatically.\n\n"
        f"**Conflicted files:**\n{files}\n\n"
        f"**Conflict preview:**\n```\n{preview}\n```\n\n"
        "Please resolve the conflicts manually and push, or adjust the "
        "dependency order so this branch doesn't conflict with main."
    )


async def run_rebase_round(
    active_sessions: dict[str, Any],
    main_sha: str,
    repo_url: str,
    token: str,
    *,
    trigger_pr_number: int = 0,
    notification_service: Any = None,
    github: Any = None,
    human_reviewers: list[str] | None = None,
) -> RebaseRound:
    """Orchestrate a full rebase round for all stale in-flight branches.

    Returns a ``RebaseRound`` with the outcome per branch.
    """
    rr = RebaseRound(
        trigger_pr_number=trigger_pr_number,
        trigger_sha=main_sha,
    )

    stale = detect_stale_branches(active_sessions, main_sha)
    if not stale:
        logger.info("rebase.no_stale_branches")
        return rr

    logger.info(
        "rebase.round_starting",
        branch_count=len(stale),
        main_sha=main_sha[:8],
    )

    for entry in stale:
        card_id = entry["card_id"]
        branch = entry["branch"]
        pr_number = entry.get("pr_number", 0)

        # FR-006: active-performer branches are pre-marked as skipped
        if entry.get("skipped"):
            job = RebaseJob(
                card_id=card_id, branch=branch, pr_number=pr_number,
                target_main_sha=main_sha, outcome=RebaseOutcome.SKIPPED,
            )
            rr.jobs.append(job)
            continue

        job = await rebase_branch(repo_url, branch, main_sha, token)
        job.card_id = card_id
        job.pr_number = pr_number

        if job.outcome == RebaseOutcome.CLEAN:
            # Force-push the cleanly rebased branch
            repo_dir = job.repo_dir
            tmp_dir = job.tmp_dir
            if repo_dir:
                success = await force_push_with_lease(
                    repo_dir, branch, job.pre_rebase_sha, token,
                )
                if not success:
                    job.outcome = RebaseOutcome.FAILED
                    job.conflict_preview = "Force-push-with-lease failed after rebase"
            # Clean up temp dir
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)

            logger.info(
                "rebase.clean",
                card_id=card_id,
                branch=branch,
                new_sha=job.post_rebase_sha[:8],
                duration=job.duration_seconds,
            )
        elif job.outcome == RebaseOutcome.BLOCKED:
            logger.warning(
                "rebase.conflict_detected",
                card_id=card_id,
                branch=branch,
                conflicted_files=job.conflicted_files,
            )
            # NOTE: diagnostic comment is NOT posted here.  The caller
            # (merge_pr / check_board) first attempts performer-driven
            # conflict resolution via prepare_conflict_resolution().
            # Only if the performer fails does the caller post the
            # diagnostic comment using build_conflict_block_comment().
        elif job.outcome == RebaseOutcome.SKIPPED:
            logger.info("rebase.already_up_to_date", card_id=card_id, branch=branch)
        else:
            logger.error(
                "rebase.failed",
                card_id=card_id,
                branch=branch,
                error=job.conflict_preview[:200],
            )

        rr.jobs.append(job)

    # Post Slack summary
    if notification_service is not None and any(
        j.outcome != RebaseOutcome.SKIPPED for j in rr.jobs
    ):
        try:
            from coordinare.models.notification import (
                EventType,
                NotificationEvent,
                NotificationSeverity,
            )
            event = NotificationEvent(
                event_type=EventType.card_transition,  # reuse until dedicated type added
                severity=NotificationSeverity.info,
                payload={
                    "event_type": "rebase_round_complete",
                    "summary": f"🔄 Rebase round: {rr.summary}",
                    "source": "rebase",
                },
                source="rebase",
                dedup_key=f"rebase_round:{main_sha[:8]}",
            )
            await notification_service.dispatch(event)
        except Exception as exc:
            logger.warning("rebase.notification_failed", error=str(exc))

    logger.info(
        "rebase.round_complete",
        job_count=len(rr.jobs),
        summary=rr.summary,
    )
    return rr
