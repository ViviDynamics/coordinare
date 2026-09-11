"""The model reads the repository and says what it is (#367).

Two workflows asked that question from a fixed list: the documenter probed for
``pyproject.toml`` and ``package.json`` and mapped the first hit to ``pytest`` or
``npm test``; QA branched Rails, then Django, then Node, and named those three in
its own failure message.

These tests use the REAL ``Toolkit``, not a hand-written stand-in with a
``call_model`` of its own shape. In spec 173 a FakeToolkit with the wrong
``call_model`` signature let 44 tests pass against a contract the production code
did not have, and only the eval -- which uses the real Toolkit -- caught it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from performer.workflows.base import WorkflowMetrics
from performer.workflows.budget import ModelReply
from performer.workflows.project_shape import (
    MAX_MANIFEST_BYTES,
    ProjectShape,
    ProjectShapeUnknown,
    detect_shape,
    shape_persona,
)
from performer.workflows.toolkit import Toolkit

SHAPE = {
    "project_name": "week",
    "summary": "a Rails API with a React front end",
    "test_command": "bundle exec rspec",
    "start_command": "bin/rails server -b 127.0.0.1 -p $PORT",
    "boot_seconds": 90,
    "source_dirs": ["app", "lib"],
    "cannot_determine": "",
}


def _toolkit(reply: dict, *, capture: dict | None = None) -> Toolkit:
    """The real Toolkit, with only the gateway replaced."""

    async def model_call(persona, content, max_tokens):
        if capture is not None:
            capture["persona"] = persona
            capture["text"] = "".join(c.get("text", "") for c in content)
            capture["max_tokens"] = max_tokens
        return ModelReply(content=json.dumps(reply), finish_reason="stop")

    return Toolkit(metrics=WorkflowMetrics(), model_call=model_call, call_limit=8)


def _repo(tmp_path: Path, files: dict[str, str]) -> tuple[Path, set[str]]:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return tmp_path, set(files)


class TestTheModelDecidesAndCoordinareDoesNot:
    @pytest.mark.asyncio
    async def test_a_stack_coordinare_never_heard_of_needs_no_code_change(self, tmp_path: Path) -> None:
        """The point of the change, stated as a test.

        Nothing here is Python or Node. The old probes would have produced an
        empty layout and a blank test hint.
        """
        workspace, tree = _repo(tmp_path, {
            "mix.exs": 'defmodule Week.MixProject do\n  def project, do: [app: :week]\nend\n',
            "lib/week.ex": "defmodule Week do\nend\n",
            "test/week_test.exs": "defmodule WeekTest do\nend\n",
        })
        shape = await detect_shape(_toolkit({
            **SHAPE, "project_name": "week", "summary": "an Elixir library",
            "test_command": "mix test", "start_command": "", "source_dirs": ["lib"],
        }), workspace, tree)
        assert shape.test_command == "mix test"
        assert shape.start_command == "", "a library is not something you browse"

    @pytest.mark.asyncio
    async def test_the_persona_names_no_language_and_no_file(self) -> None:
        """A persona that lists the stacks it knows is the table, relocated."""
        persona = shape_persona().lower()
        for word in ("python", "ruby", "rails", "django", "node", "npm", "pytest",
                     "pyproject", "package.json", "gemfile", "go.mod", "cargo"):
            assert word not in persona, f"the persona names {word!r}"

    @pytest.mark.asyncio
    async def test_the_model_is_shown_root_files_whatever_they_are_called(self, tmp_path: Path) -> None:
        """Not a list of known manifest names: every root file it can read.

        The interesting file might be mix.exs, a Justfile, or something drafted
        after this was written.
        """
        capture: dict = {}
        workspace, tree = _repo(tmp_path, {
            "Justfile": "test:\n\tmix test\n",
            "deno.json": '{"tasks": {"test": "deno test"}}\n',
            "src/app.ts": "export const x = 1\n",
        })
        await detect_shape(_toolkit(SHAPE, capture=capture), workspace, tree)
        assert "Justfile" in capture["text"] and "mix test" in capture["text"]
        assert "deno.json" in capture["text"] and "deno test" in capture["text"]

    @pytest.mark.asyncio
    async def test_an_oversized_root_file_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        """A lockfile at the root must not blow the budget or the call."""
        capture: dict = {}
        workspace, tree = _repo(tmp_path, {
            "Gemfile.lock": "x" * (MAX_MANIFEST_BYTES + 1),
            "Gemfile": "source 'https://rubygems.org'\n",
        })
        await detect_shape(_toolkit(SHAPE, capture=capture), workspace, tree)
        assert "rubygems" in capture["text"], "the small file was still read"
        assert "x" * 500 not in capture["text"], "the oversized one was not"


class TestARepositoryNobodyUnderstandsIsAStop:
    """The floor #367 asks both workflows to keep, taken from QA.

    QA was the only workflow in the #364 inventory that failed loudly rather
    than degrading. An empty picture is never a result.
    """

    @pytest.mark.asyncio
    async def test_cannot_determine_raises_rather_than_returning_an_empty_shape(self, tmp_path: Path) -> None:
        workspace, tree = _repo(tmp_path, {"README": "a directory of files\n"})
        with pytest.raises(ProjectShapeUnknown, match="no manifest"):
            await detect_shape(
                _toolkit({**SHAPE, "cannot_determine": "no manifest, no source layout I recognise"}),
                workspace, tree,
            )

    @pytest.mark.asyncio
    async def test_the_operator_gets_the_models_own_reason(self, tmp_path: Path) -> None:
        """A hold nobody can act on is only marginally better than a wrong answer."""
        workspace, tree = _repo(tmp_path, {"README": "x\n"})
        with pytest.raises(ProjectShapeUnknown) as exc:
            await detect_shape(
                _toolkit({**SHAPE, "cannot_determine": "the build is driven by a Makefile I cannot read"}),
                workspace, tree,
            )
        assert "Makefile I cannot read" in exc.value.reason

    @pytest.mark.asyncio
    async def test_an_empty_command_is_an_answer_and_not_a_stop(self, tmp_path: Path) -> None:
        """The distinction that makes the stop meaningful.

        "This is a Go library with no server" is a successful characterisation.
        Treating it as a stop would park every library card coordinare sees.
        """
        workspace, tree = _repo(tmp_path, {"go.mod": "module week\n", "week.go": "package week\n"})
        shape = await detect_shape(_toolkit({
            **SHAPE, "summary": "a Go library", "test_command": "go test ./...",
            "start_command": "", "cannot_determine": "",
        }), workspace, tree)
        assert shape.start_command == "" and shape.test_command == "go test ./..."

    @pytest.mark.asyncio
    async def test_whitespace_is_not_a_reason(self, tmp_path: Path) -> None:
        """A model that sets the field to ' ' must not stop the card silently."""
        workspace, tree = _repo(tmp_path, {"go.mod": "module week\n"})
        shape = await detect_shape(_toolkit({**SHAPE, "cannot_determine": "   "}), workspace, tree)
        assert isinstance(shape, ProjectShape)
