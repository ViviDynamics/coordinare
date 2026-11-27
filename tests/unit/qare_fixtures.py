"""404: shared fixtures for the QAre provider tests: a repository to fetch, a fake QAre."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from tests.unit import fake_qare

FAKE_QARE = [sys.executable, str(Path(fake_qare.__file__).resolve())]
CRITERIA = [
    {"id": "ac-111111111111", "text": "The payouts page shows the 1099 notice"},
    {"id": "ac-222222222222", "text": "The tax test passes"},
]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@example.com", "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
             "HOME": str(cwd)},
    ).stdout.strip()


def make_remote(tmp: Path, *, profile_on_base: bool = True, profile_on_head: bool = False) -> tuple[str, str, str]:
    """A bare repository ``acme/widgets`` under ``tmp/remote``. Returns (web_base, base, head)."""
    work = tmp / "work"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "app.txt").write_text("v1\n")
    if profile_on_base:
        (work / ".qa").mkdir()
        (work / ".qa" / "config.yml").write_text("app: {}\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "base")
    base = git(work, "rev-parse", "HEAD")
    (work / "app.txt").write_text("v2\n")
    if profile_on_head:
        (work / ".qa").mkdir(exist_ok=True)
        (work / ".qa" / "config.yml").write_text("app: {}\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "head")
    head = git(work, "rev-parse", "HEAD")
    remote = tmp / "remote" / "acme"
    remote.mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(remote / "widgets.git")], check=True)
    git(remote / "widgets.git", "config", "uploadpack.allowReachableSHA1InWant", "true")
    git(remote / "widgets.git", "config", "uploadpack.allowAnySHA1InWant", "true")
    return f"file://{tmp / 'remote'}", base, head


def scenario(tmp: Path, **values: Any) -> Path:
    path = tmp / "scenario.json"
    path.write_text(json.dumps(values))
    return path


def calls(path: Path) -> list[dict[str, Any]]:
    log = Path(f"{path}.calls.jsonl")
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def request_spec(job_dir: Path, *, web_base: str, base: str, head: str, **overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "request_id": job_dir.name,
        "card_id": "PVTI_card", "repository": "acme/widgets", "pr_number": 7, "pr_node_id": "PR_7",
        "issue_number": 5, "issue_node_id": "I_5",
        "head_sha": head, "base_sha": base, "base_ref": "main",
        "criteria": CRITERIA,
        "api_base": "http://127.0.0.1:9", "web_base": web_base,
        "command": FAKE_QARE, "version": fake_qare.VERSION, "profile": ".qa",
        "env_passthrough": ["FAKE_QARE_SCENARIO"], "timeout_seconds": 120,
    }
    spec.update(overrides)
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "request.json").write_text(json.dumps(spec))
    return spec
