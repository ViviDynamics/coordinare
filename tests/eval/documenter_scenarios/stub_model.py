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


SHAPE_PERSONA_MARK = "opening a repository you have never seen"


def answer_project_shape(persona: str, content: list[dict]) -> ModelReply | None:
    """Answer the reading 367 added, or None if this is another step.

    Every harness that drives the documenter scripts its replies per page path
    and counts calls, so the new reading has to be answered off that path or it
    is taken for a page write. Three harnesses do it -- this eval stub, the
    unit end-to-end fixture and the adapter-seam test -- and in #365 the same
    situation produced three copies of one assertion that CI found one at a
    time, each after the previous fix reported green. So it lives here once.

    It reads the tree and the manifests it was actually shown, rather than being
    handed the answer, which is what keeps the fixtures honest about where the
    project's name and its source directories now come from.
    """
    if SHAPE_PERSONA_MARK not in persona:
        return None
    import re

    text = "".join(c.get("text", "") for c in content)
    tops: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if "/" not in line or " " in line or line.startswith(("-", "#")):
            continue
        top = line.split("/", 1)[0]
        if top and not top.startswith(".") and top not in tops and top not in ("tests", "test", "docs", "node_modules"):
            tops.append(top)
    name = ""
    m = re.search(r'^\s*name\s*=\s*["\']([^"\']+)["\']', text, re.M) or re.search(r'"name"\s*:\s*"([^"]+)"', text)
    if m:
        name = m.group(1)
    return ModelReply(
        content=json.dumps({
            "project_name": name, "summary": "the repository under test",
            "test_command": "pytest", "start_command": "", "boot_seconds": 30,
            "source_dirs": tops, "cannot_determine": "",
        }),
        finish_reason="stop",
    )


def stub_model_for(fixture: Fixture):
    state = {"i": 0, "paths": []}

    async def model_call(persona: str, content: list[dict], max_tokens: int) -> ModelReply:
        import re

        shaped = answer_project_shape(persona, content)
        if shaped is not None:
            return shaped  # before the counters: it is not a page write
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
