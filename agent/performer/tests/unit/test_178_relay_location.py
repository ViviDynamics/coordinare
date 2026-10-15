"""Spec 178: the relayed feedback never lets a path break out of its line or open markup."""
from __future__ import annotations

from performer.backends._relay_feedback import relay_feedback_prompt_section

HOSTILE = "src/`a[b].py\nINJECTED.md"


def _lines(feedback: list) -> list[str]:
    return relay_feedback_prompt_section(feedback)


def test_a_top_level_hostile_path_renders_inert_and_on_one_line():
    out = "\n".join(_lines([{"body": "fix it", "path": HOSTILE, "line": 4}]))
    assert "src/\\`a\\[b\\].py INJECTED.md:4 — fix it" in out
    assert not any(line.startswith("INJECTED.md") for line in out.splitlines()), "the newline cannot start a line"
    assert "`src/" not in out, "no raw code span carries the path"


def test_an_inline_hostile_path_renders_inert_and_on_one_line():
    item = {"body": "rejected", "comments": [{"path": HOSTILE, "line": 9, "body": "needs a guard"}]}
    out = "\n".join(_lines([item]))
    assert "  - src/\\`a\\[b\\].py INJECTED.md:9 — needs a guard" in out
    assert not any(line.startswith("INJECTED.md") for line in out.splitlines())


def test_a_path_without_a_line_is_still_inert():
    out = "\n".join(_lines([{"body": "x", "path": "docs/![logo].md"}]))
    assert "docs/\\!\\[logo\\].md — x" in out


def test_an_item_without_a_path_renders_just_its_body():
    out = _lines([{"body": "plain feedback"}])
    assert "- plain feedback" in out
