"""Deterministic stand-ins for the documenter eval: a model answering per page path
(with a generic page per kind for init mode), and the local committer."""
from __future__ import annotations

import json

from performer.workflows.budget import ModelReply

from tests.eval.documenter_scenarios.fixtures import Fixture

_FILLER = "The code in `src/app.py` handles requests and `src/db.py` stores data. " * 6


def generic_page(kind: str, title: str) -> str:
    if kind == "explanation":
        body = f"## What it is\n{_FILLER}\n\n## How it fits\n{_FILLER}\n\n## Why it is this way\n{_FILLER}\n\n## Where to change it\nStart from `src/app.py`; tests in `tests/test_app.py`.\n"
    elif kind == "how-to":
        body = f"## Goal\n{_FILLER}\n\n## Prerequisites\n`pyproject.toml`.\n\n## Steps\n1. Run:\n\n```bash\npytest\n```\n\n## Verify\n{_FILLER}\n"
    else:
        body = f"| Name | What it is |\n| --- | --- |\n| `src/app.py` | the app |\n| `src/db.py` | storage |\n\n{_FILLER}\n"
    return f"---\nkind: {kind}\n---\n# {title}\n\n{body}"


def stub_model_for(fixture: Fixture):
    state = {"i": 0, "paths": []}

    async def model_call(persona: str, content: list[dict], max_tokens: int) -> ModelReply:
        import re

        state["i"] += 1
        m = re.search(r"Write the page `([^`]+)`", persona)
        path = m.group(1) if m else "?"
        state["paths"].append(path)
        if path in fixture.replies:
            return ModelReply(content=json.dumps(fixture.replies[path]), finish_reason="stop")
        if fixture.mode == "init":
            k = re.search(r"kind: (\w[\w-]*)", persona)
            kind = k.group(1) if k else "reference"
            title = path.rsplit("/", 1)[-1].removesuffix(".md").replace("-", " ").capitalize()
            return ModelReply(content=json.dumps({"action": "write", "content": generic_page(kind, title), "reason": ""}), finish_reason="stop")
        return ModelReply(content=json.dumps({"action": "unchanged", "content": "", "reason": "nothing to add"}), finish_reason="stop")

    model_call.state = state
    return model_call
