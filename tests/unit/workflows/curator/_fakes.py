"""Fakes for the curator workflow tests (spec 173)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from performer.workflows.base import WorkflowMetrics


class FakeBoard:
    def __init__(self, on_board: set[str] | None = None, *, fail_add: bool = False) -> None:
        self._on_board = on_board or set()
        self._fail_add = fail_add
        self.added: list[str] = []
        self.labels: list[tuple[str, str]] = []
        self.comments: list[tuple[int, str]] = []
        self.moved: list[tuple[str, str]] = []
        self.columns_set: list[tuple[str, str]] = []

    async def on_board_ids(self) -> set[str]:
        return set(self._on_board)

    async def add(self, issue_id: str) -> str:
        if self._fail_add:
            raise RuntimeError("board unreachable")
        self.added.append(issue_id)
        return f"PVTI_{issue_id}"

    async def label(self, issue_id: str, label: str) -> None:
        self.labels.append((issue_id, label))

    async def comment(self, number: int, body: str) -> None:
        self.comments.append((number, body))

    async def move(self, item_id: str, column: str) -> None:
        self.moved.append((item_id, column))

    async def set_column(self, item_id: str, column: str) -> None:
        self.columns_set.append((item_id, column))


class FakeToolkit:
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


def issue(n: int, *, title: str = "Add a health endpoint",
          body: str = "Acceptance: GET /health returns 200.", labels: list[str] | None = None) -> dict:
    return {"id": f"I_{n}", "number": n, "title": title, "body": body,
            "url": f"https://github.com/o/r/issues/{n}", "labels": labels or []}


def judgement(issue_id: str = "I_1", **over: Any) -> dict:
    base = {"issue_id": issue_id, "qualifies": True,
            "reason": "it states a testable acceptance criterion",
            "quote": "GET /health returns 200."}
    base.update(over)
    return base


def batch(*judgements: dict) -> dict:
    return {"judgements": list(judgements)}


def score(**over: Any) -> Any:
    from performer.models import Score

    base = {"title": "curator run", "repo_url": "https://github.com/o/r",
            "branch": "curator/s", "role": "curator", "workflow": "curator",
            "project_id": "PVT_1"}
    base.update(over)
    return Score(**base)


def stand(tmp_path: Path) -> Any:
    return SimpleNamespace(path=tmp_path, branch="curator/s")
