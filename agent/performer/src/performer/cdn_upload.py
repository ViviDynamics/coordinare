"""GitHub CDN screenshot upload helper (performer side).

Uploads screenshots by committing them to an orphan ``qa-assets`` branch
under ``content/<issue_number>/<UTC timestamp>-<sanitized name>``. The
returned URL is the raw GitHub URL for that path, which renders inline
in PR/issue comments.

Why a branch instead of the REST API: GitHub's ``/asset-upload-url``
endpoint that the web UI uses is not available to Bearer-token clients
(returns 404); the only reliable, supported way to host arbitrary
binary assets on GitHub from automation is to push them to a branch.

Authentication: the token is supplied to git via ``GIT_ASKPASS`` rather
than embedded in the remote URL. git logs the remote URL on failure, so
embedding the token there would leak it through subprocess stderr.

Mirrors ``src/coordinare/services/cdn_upload.py`` so each side can be
deployed independently. Keep changes in sync — both files share the
same authentication scheme and URL format.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import structlog

logger = structlog.get_logger(__name__)

_DEFAULT_MAX_RETRIES = 3
_RETRY_BASE_S = 1.0
_BRANCH = "qa-assets"
_BOT_EMAIL = "qa-bot@coordinare.local"
_BOT_NAME = "Coordinare QA Bot"

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

GitRunner = Callable[
    [list[str], Path, "dict[str, str] | None"],
    Awaitable[tuple[int, str, str]],
]


def is_local_path(loc: str) -> bool:
    """Return True if ``loc`` looks like a local filesystem path rather
    than an http(s) URL. Empty strings return False.
    """
    if not loc:
        return False
    lowered = loc.lower()
    if lowered.startswith(("http://", "https://")):
        return False
    return True


def is_image_path(loc: str) -> bool:
    return Path(loc).suffix.lower() in _IMAGE_SUFFIXES


def _sanitize_name(name: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("-", name).strip("-.")
    return cleaned or "file"


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _redact(text: str, token: str) -> str:
    if not token:
        return text
    return text.replace(token, "***")


async def _default_runner(
    argv: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    out, err = await proc.communicate()
    return (
        proc.returncode if proc.returncode is not None else -1,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


def _write_askpass(tmp_root: Path) -> Path:
    """Write an askpass helper that echoes the token from env."""
    askpass = tmp_root / "askpass.sh"
    askpass.write_text(
        '#!/bin/sh\nprintf "%s" "$COORDINARE_GH_TOKEN"\n',
    )
    askpass.chmod(0o700)
    return askpass


def _git_env(token: str, askpass: Path) -> dict[str, str]:
    return {
        **os.environ,
        "GIT_ASKPASS": str(askpass),
        "COORDINARE_GH_TOKEN": token,
        "GIT_TERMINAL_PROMPT": "0",
    }


async def upload_screenshot(
    file_path: Path,
    github_token: str,
    org: str,
    repo: str,
    issue_number: int,
    *,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    _runner: GitRunner | None = None,
) -> str | None:
    """Commit ``file_path`` to ``qa-assets`` branch and return its raw URL.

    Returns ``None`` on any failure. Retries on push contention (non
    fast-forward) by re-cloning, since each commit adds a uniquely
    timestamped file.
    """
    if not file_path.exists():
        logger.warning("cdn_upload.file_not_found", path=str(file_path))
        return None
    if not github_token or not org or not repo or issue_number <= 0:
        logger.warning("cdn_upload.missing_context")
        return None

    runner = _runner or _default_runner
    asset_path = f"content/{issue_number}/{_timestamp()}-{_sanitize_name(file_path.name)}"
    # Username in URL is fine (public); password supplied via GIT_ASKPASS.
    remote_url = f"https://x-access-token@github.com/{org}/{repo}.git"

    for attempt in range(1, max_retries + 1):
        tmp_root = Path(tempfile.mkdtemp(prefix="cdn-upload-"))
        work = tmp_root / "repo"
        askpass = _write_askpass(tmp_root)
        env = _git_env(github_token, askpass)
        try:
            clone_rc, _, clone_err = await runner(
                ["git", "clone", "--depth=1", "--single-branch",
                 "--branch", _BRANCH, remote_url, str(work)],
                tmp_root,
                env,
            )
            if clone_rc != 0:
                work.mkdir(parents=True, exist_ok=True)
                init_rc, _, init_err = await runner(
                    ["git", "init", "-b", _BRANCH], work, env,
                )
                if init_rc != 0:
                    logger.warning(
                        "cdn_upload.init_failed",
                        stderr=_redact(init_err, github_token),
                    )
                    return None
                remote_rc, _, remote_err = await runner(
                    ["git", "remote", "add", "origin", remote_url], work, env,
                )
                if remote_rc != 0:
                    logger.warning(
                        "cdn_upload.remote_failed",
                        stderr=_redact(remote_err, github_token),
                    )
                    return None

            await runner(["git", "config", "user.email", _BOT_EMAIL], work, env)
            await runner(["git", "config", "user.name", _BOT_NAME], work, env)

            dest = work / asset_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(file_path.read_bytes())

            add_rc, _, add_err = await runner(["git", "add", asset_path], work, env)
            if add_rc != 0:
                logger.warning(
                    "cdn_upload.add_failed",
                    stderr=_redact(add_err, github_token),
                )
                return None

            commit_rc, _, commit_err = await runner(
                ["git", "commit", "-m", f"qa: upload {asset_path}"], work, env,
            )
            if commit_rc != 0:
                logger.warning(
                    "cdn_upload.commit_failed",
                    stderr=_redact(commit_err, github_token),
                )
                return None

            push_rc, _, push_err = await runner(
                ["git", "push", "origin", f"HEAD:refs/heads/{_BRANCH}"], work, env,
            )
            if push_rc != 0:
                logger.warning(
                    "cdn_upload.push_failed",
                    attempt=attempt,
                    stderr=_redact(push_err, github_token),
                )
                if attempt < max_retries:
                    await asyncio.sleep(_RETRY_BASE_S * (2 ** (attempt - 1)))
                    continue
                return None

            cdn_url = (
                f"https://github.com/{org}/{repo}/raw/{_BRANCH}/{asset_path}"
            )
            logger.info(
                "cdn_upload.ok",
                file=file_path.name,
                cdn_url=cdn_url,
                attempt=attempt,
            )
            return cdn_url
        except Exception as exc:
            logger.warning(
                "cdn_upload.error",
                attempt=attempt,
                error=_redact(str(exc), github_token),
            )
            if attempt < max_retries:
                await asyncio.sleep(_RETRY_BASE_S * (2 ** (attempt - 1)))
            else:
                return None
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

    return None


async def resolve_visual_evidence_urls(
    visual_evidence: list[dict[str, str]],
    *,
    workspace_root: Path,
    github_token: str,
    org: str,
    repo: str,
    issue_number: int,
    uploader: Any = None,
) -> list[dict[str, str]]:
    """Replace container-local ``path_or_url`` entries with CDN URLs.

    For each entry in ``visual_evidence``:
      - Leave http(s) URLs alone.
      - For non-URL values, resolve against ``workspace_root`` (if relative)
        or use the path as-is (if absolute), and try to upload.
        On success, replace ``path_or_url`` with the returned URL.
        On failure, leave the entry unchanged.

    ``uploader`` is injected for tests.
    """
    if not visual_evidence:
        return visual_evidence
    if issue_number <= 0 or not github_token or not org or not repo:
        return visual_evidence

    upload = uploader or upload_screenshot
    out: list[dict[str, str]] = []
    for ev in visual_evidence:
        ev2 = dict(ev)
        loc = str(ev2.get("path_or_url", ""))
        if loc and is_local_path(loc):
            candidate = Path(loc)
            if not candidate.is_absolute():
                candidate = workspace_root / candidate
            try:
                url = await upload(
                    candidate, github_token, org, repo, issue_number,
                )
            except Exception as exc:
                logger.warning(
                    "cdn_upload.evidence_upload_failed",
                    path=str(candidate),
                    error=_redact(str(exc), github_token),
                )
                url = None
            if url:
                ev2["path_or_url"] = url
        out.append(ev2)
    return out
