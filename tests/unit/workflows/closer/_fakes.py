"""A fake GitHub for the closer tests (spec 172): threads in, resolutions recorded."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any


def comment(author: str, body: str, at: str = "2026-09-07T10:00:00Z") -> dict:
    return {"author": author, "body": body, "created_at": at}


def thread(tid: str, *comments: dict, resolved: bool = False, outdated: bool = False, path: str = "src/app.py", line: int = 10) -> dict:
    return {"id": tid, "path": path, "line": line, "resolved": resolved, "outdated": outdated, "comments": list(comments)}


class FakeGitHub:
    """Records what the workflow fetched, posted and resolved."""

    def __init__(self, threads: list[dict], *, pages: int = 1, fetch_error: str | None = None,
                 post_error: str | None = None, resolve_failures: set[str] | None = None,
                 review_context: dict | None = None) -> None:
        self.threads, self.pages = threads, pages
        self.fetch_error, self.post_error = fetch_error, post_error
        self.resolve_failures = resolve_failures or set()
        self.review_context = review_context or {}
        self.reviews: list[dict] = []
        self.resolved: list[str] = []

    async def fetcher(self, score, budgets):
        if self.fetch_error:
            raise RuntimeError(self.fetch_error)
        from performer.workflows.closer.models import Thread

        return [Thread.model_validate(t) for t in self.threads], self.pages, dict(self.review_context)

    async def resolver(self, score, ids: list[str]):
        ok = [i for i in ids if i not in self.resolve_failures]
        self.resolved.extend(ok)
        failures = [{"id": i, "error": "could not resolve"} for i in ids if i in self.resolve_failures]
        return ok, failures

    async def poster(self, owner, repo, number, *, event, body, comments, token):
        if self.post_error:
            raise RuntimeError(self.post_error)
        self.reviews.append({"event": event, "body": body, "comments": comments, "number": number})
        return {"html_url": f"https://github.com/{owner}/{repo}/pull/{number}#review-{len(self.reviews)}"}


def score(**over: Any) -> SimpleNamespace:
    base = {"pr_url": "https://github.com/o/r/pull/7", "owner_repo": ("o", "r"), "effective_github_token": "tok",
                "backend": "codex", "model": "m", "workflow_env": {}, "head_sha": "abc1234"}
    base.update(over)
    return SimpleNamespace(**base)
