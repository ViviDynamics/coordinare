"""CLI shims that expose performer helpers to the in-container agent.

These commands are installed by ``pip install .`` (see ``pyproject.toml``
``[project.scripts]``) and end up on ``$PATH`` inside every performer
Docker image, so any backend (claude_code, opencode, codex, junie,
cursor) can shell out to them via its built-in shell-execution tool.

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


def main() -> None:  # pragma: no cover - thin wrapper
    sys.exit(upload_screenshot_cli())


if __name__ == "__main__":  # pragma: no cover
    main()
