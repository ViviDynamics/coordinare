"""Fakes for the advocate workflow tests (spec 173). No network, ever."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from performer.workflows.base import WorkflowMetrics


class FakeGitHub:
    """Records every label and comment instead of sending them."""

    def __init__(self, issues: list[dict], *, fail_list: bool = False,
                 fail_label_on: set[str] | None = None) -> None:
        self._issues = issues
        self._fail_list = fail_list
        self._fail_label_on = fail_label_on or set()
        self.labels: list[tuple[str, str]] = []
        self.comments: list[tuple[int, str]] = []

    async def lister(self) -> list[dict]:
        if self._fail_list:
            raise RuntimeError("github is down")
        return list(self._issues)

    # the Poster surface
    async def label(self, issue_id: str, label: str) -> None:
        if issue_id in self._fail_label_on:
            raise RuntimeError("label failed")
        self.labels.append((issue_id, label))

    async def comment(self, issue_number: int, body: str) -> None:
        self.comments.append((issue_number, body))

    def labels_for(self, issue_id: str) -> list[str]:
        return [lab for iid, lab in self.labels if iid == issue_id]


class FakeToolkit:
    """A toolkit whose model returns queued payloads and counts the calls."""

    def __init__(self, replies: list[Any] | None = None) -> None:
        self.metrics = WorkflowMetrics()
        self._replies = list(replies or [])
        self.calls: list[tuple[str, str]] = []
        self.commands: list[str] = []
        self._events: list[Any] = []

    async def call_model(self, *, persona: str, schema, content, budget=None):
        """Mirrors the REAL Toolkit signature exactly: keyword-only, schema in,
        validated model out.

        This fake previously took (persona, content, max_tokens) positionally
        and returned a string, which is not the contract. Every test passed
        against a shape that does not exist, and the eval caught it because the
        eval uses the real Toolkit. A fake that does not match the seam it
        replaces proves nothing.
        """
        self.calls.append((persona, content))
        self.metrics.model_calls += 1
        payload = self._replies.pop(0) if self._replies else {}
        if isinstance(payload, str):
            payload = json.loads(payload)
        return schema.model_validate(payload)

    async def run_command(self, cmd: str, **kw: Any) -> Any:
        self.commands.append(cmd)
        return SimpleNamespace(stdout="", stderr="", exit_code=0)

    def emit(self, event: Any) -> None:
        self._events.append(event)

    @property
    def events(self) -> list[Any]:
        return self._events


def issue(n: int, *, title: str = "How do I start?", body: str = "Tell me.",
          labels: list[str] | None = None) -> dict:
    return {
        "id": f"I_{n}", "number": n, "title": title, "body": body,
        "url": f"https://github.com/o/r/issues/{n}", "labels": labels or [],
    }


def classification(issue_id: str = "I_1", **over: Any) -> dict:
    base = {
        "issue_id": issue_id,
        "classification": "question",
        "confidence": 0.9,
        "reasoning": "the readme covers it",
        "answer": "Based on `README.md`: run make start.",
        "cited_documents": ["README.md"],
    }
    base.update(over)
    return base


def batch(*classifications: dict) -> dict:
    return {"classifications": list(classifications)}


def score(**over: Any) -> Any:
    from performer.models import Score

    base = {
        "title": "advocate run",
        "repo_url": "https://github.com/o/r",
        "branch": "advocate/s",
        "role": "advocate",
        "workflow": "advocate",
    }
    base.update(over)
    return Score(**base)


def stand(tmp_path: Path, docs: dict[str, str] | None = None) -> Any:
    for rel, content in (docs or {}).items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return SimpleNamespace(path=tmp_path, branch="advocate/s")
