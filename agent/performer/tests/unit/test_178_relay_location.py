"""Spec 178: the relayed feedback never lets a path break out of its line.

The relay is model-facing, not GitHub-rendered, so a path is flattened and capped
rather than markup-escaped (escaping would change the name the model must match).
"""
from __future__ import annotations

from performer.backends._relay_feedback import relay_feedback_prompt_section
from performer.workflows.untrusted import PATH_LIMIT

HOSTILE = "src/`a[b].py\nINJECTED.md"


def _text(feedback: list) -> str:
    return "\n".join(relay_feedback_prompt_section(feedback))


def test_a_top_level_hostile_path_stays_on_one_line():
    out = _text([{"body": "fix it", "path": HOSTILE, "line": 4}])
    assert "- src/`a[b].py INJECTED.md:4 — fix it" in out
    assert not any(line.startswith("INJECTED.md") for line in out.splitlines()), "the newline cannot start a line"


def test_an_inline_hostile_path_stays_on_one_line():
    item = {"body": "rejected", "comments": [{"path": HOSTILE, "line": 9, "body": "needs a guard"}]}
    out = _text([item])
    assert "  - src/`a[b].py INJECTED.md:9 — needs a guard" in out
    assert not any(line.startswith("INJECTED.md") for line in out.splitlines())


def test_a_path_without_a_line_is_flat():
    out = _text([{"body": "x", "path": "docs/[logo]\n.md"}])
    assert "- docs/[logo] .md — x" in out


def test_a_long_path_is_capped():
    out = _text([{"body": "x", "path": "d/" * PATH_LIMIT}])
    assert ("d/" * PATH_LIMIT)[: PATH_LIMIT + 1] not in out


def test_an_item_without_a_path_renders_just_its_body():
    assert "- plain feedback" in _text([{"body": "plain feedback"}])
