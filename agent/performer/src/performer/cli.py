"""CLI shims that expose performer helpers to the in-container agent.

These commands are installed by ``pip install .`` (see ``pyproject.toml``
``[project.scripts]``) and end up on ``$PATH`` inside every performer
Docker image, so any backend (claude_code, opencode, codex, junie)
can shell out to them via its built-in shell-execution tool.

Context is passed via environment variables that the performer wrapper
exports when launching the backend (see ``workspace.py`` —
``stand.cache_env`` is merged into the subprocess env).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from performer.cdn_upload import upload_screenshot
from performer.github import (
    GitHubAPIError,
    get_check_run_logs,
    get_check_runs,
    get_pr_head_sha,
)


def upload_screenshot_cli(argv: list[str] | None = None) -> int:
    """Upload a local screenshot/image to GitHub's user-attachments CDN
    and print the resulting embeddable URL to stdout on success.

    Usage:
        performer-upload-screenshot <path>
        performer-upload-screenshot --label "After fix" <path>

    Reads context from env vars (set by the performer wrapper):
        PERFORMER_GH_TOKEN   — GitHub token with PR/issue write access
        PERFORMER_GH_OWNER   — owner of the target repo
        PERFORMER_GH_REPO    — repo name
        PERFORMER_GH_ISSUE   — issue/PR number for upload scoping

    Exit codes:
        0  success — URL printed to stdout
        1  bad usage / missing context
        2  upload failed (file missing, network, GH 4xx/5xx)
    """
    parser = argparse.ArgumentParser(
        prog="performer-upload-screenshot",
        description="Upload a screenshot to GitHub CDN and print the embeddable URL.",
    )
    parser.add_argument("path", help="Path to the image file to upload")
    parser.add_argument(
        "--label",
        default="",
        help="Optional label (not embedded — for log readability only)",
    )
    args = parser.parse_args(argv)

    token = os.environ.get("PERFORMER_GH_TOKEN", "").strip()
    owner = os.environ.get("PERFORMER_GH_OWNER", "").strip()
    repo = os.environ.get("PERFORMER_GH_REPO", "").strip()
    issue_raw = os.environ.get("PERFORMER_GH_ISSUE", "").strip()
    try:
        issue_number = int(issue_raw) if issue_raw else 0
    except ValueError:
        issue_number = 0

    if not token or not owner or not repo or issue_number <= 0:
        print(
            "performer-upload-screenshot: missing context "
            "(PERFORMER_GH_TOKEN/OWNER/REPO/ISSUE not set by performer wrapper).",
            file=sys.stderr,
        )
        return 1

    path = Path(args.path)
    if not path.is_absolute():
        path = Path.cwd() / path

    try:
        url = asyncio.run(upload_screenshot(path, token, owner, repo, issue_number))
    except Exception as exc:
        print(f"performer-upload-screenshot: upload error: {exc}", file=sys.stderr)
        return 2

    if not url:
        print(
            f"performer-upload-screenshot: upload failed for {path} "
            f"(see performer logs for details).",
            file=sys.stderr,
        )
        return 2

    print(url)
    return 0


_FAILING = frozenset({"failure", "timed_out", "cancelled", "action_required"})


def _ci_context() -> tuple[str, str, str, int] | None:
    """Read CI context from env. Returns (token, owner, repo, pr_number) or None."""
    token = os.environ.get("PERFORMER_GH_TOKEN", "").strip()
    owner = os.environ.get("PERFORMER_GH_OWNER", "").strip()
    repo = os.environ.get("PERFORMER_GH_REPO", "").strip()
    raw = os.environ.get("PERFORMER_GH_ISSUE", "").strip()
    try:
        pr = int(raw) if raw else 0
    except ValueError:
        pr = 0
    if not token or not owner or not repo or pr <= 0:
        return None
    return token, owner, repo, pr


async def _list_failing(token: str, owner: str, repo: str, pr: int) -> list[dict]:
    sha = await get_pr_head_sha(owner, repo, pr, token)
    runs = await get_check_runs(owner, repo, sha, token)
    return [
        r for r in runs
        if r.get("status") == "completed" and r.get("conclusion") in _FAILING
    ]


def fetch_ci_log_cli(argv: list[str] | None = None) -> int:
    """Inspect CI failures on the current PR.

    Usage:
        performer-fetch-ci-log --list
        performer-fetch-ci-log --check "<check name>" [--lines N]

    --list                  Print failing check names + IDs (one per line).
    --check NAME            Fetch the workflow log tail for that check.
    --lines N               Tail size in characters (default 4000, max 200000).

    Reads PERFORMER_GH_TOKEN / OWNER / REPO / ISSUE (=PR number) from env.
    Exit codes: 0 success, 1 bad usage / missing context, 2 fetch failed,
    3 check name not found among failing checks.
    """
    parser = argparse.ArgumentParser(
        prog="performer-fetch-ci-log",
        description="Inspect failing CI checks on the current PR.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="List failing checks")
    group.add_argument("--check", metavar="NAME", help="Fetch log tail for a check")
    parser.add_argument("--lines", type=int, default=4000,
                        help="Tail size in chars (default 4000, max 200000)")
    args = parser.parse_args(argv)

    ctx = _ci_context()
    if ctx is None:
        print(
            "performer-fetch-ci-log: missing context "
            "(PERFORMER_GH_TOKEN/OWNER/REPO/ISSUE not set by performer wrapper).",
            file=sys.stderr,
        )
        return 1
    token, owner, repo, pr = ctx
    max_chars = max(200, min(200_000, args.lines))

    try:
        failing = asyncio.run(_list_failing(token, owner, repo, pr))
    except GitHubAPIError as exc:
        print(f"performer-fetch-ci-log: GitHub API error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"performer-fetch-ci-log: error: {exc}", file=sys.stderr)
        return 2

    if args.list:
        if not failing:
            print("(no failing checks)")
            return 0
        for r in failing:
            print(f"{r.get('id', 0)}\t{r.get('conclusion', '')}\t{r.get('name', '')}")
        return 0

    target = args.check
    match = next((r for r in failing if r.get("name") == target), None)
    if match is None:
        names = ", ".join(r.get("name", "") for r in failing) or "(none)"
        print(
            f"performer-fetch-ci-log: no failing check named {target!r}. "
            f"Failing checks: {names}",
            file=sys.stderr,
        )
        return 3

    job_id = match.get("id")
    output = match.get("output") or {}
    title = output.get("title") or ""
    summary = output.get("summary") or ""

    if title:
        print(f"## {target}: {title}")
    if summary:
        print(summary)
        print()

    if isinstance(job_id, int):
        try:
            tail = asyncio.run(
                get_check_run_logs(owner, repo, job_id, token, max_chars=max_chars),
            )
        except Exception as exc:
            print(f"performer-fetch-ci-log: log fetch error: {exc}", file=sys.stderr)
            return 2
        if tail:
            print(tail)
        else:
            print("(log unavailable — non-Actions check or unauthorized)",
                  file=sys.stderr)
    return 0


def main() -> None:  # pragma: no cover - thin wrapper
    sys.exit(upload_screenshot_cli())


def fetch_ci_log_main() -> None:  # pragma: no cover - thin wrapper
    sys.exit(fetch_ci_log_cli())


if __name__ == "__main__":  # pragma: no cover
    main()
