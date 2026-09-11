"""What kind of project is this, and how is it run (#367)?

Two workflows asked that question and answered it from a fixed list.

``documenter/inventory.py`` probed for ``pyproject.toml``, ``package.json``,
``pytest.ini`` and ``setup.cfg``, and mapped the first hit to ``pytest`` or
``npm test``. It therefore understood Python and Node. On anything else the
inventory came back empty and the documenter wrote from a blank picture, with
nothing recording that it had no idea what it was looking at.

``qa`` was more honest about the same limitation, and said so in its own words:

    no start command: infer_app_start_command recognises only Rails, Django
    and Node projects, and this is none of them.

That candour is the part worth keeping. QA was the only workflow in the #364
inventory that failed loudly instead of degrading quietly, so the floor here is
QA's posture applied to both: **a repository the model cannot characterise stops
the card and says so.** An empty picture is never a result.

What a person does on opening an unfamiliar repository is read it and work out
what it is. That is a judgement, and per #364 a judgement is the model's. It is
also what makes a stack coordinare has never seen require no code change --
which is the whole point, because the list was never going to be finished. Rails
and Django and Node were on it; Go and Rust and Elixir and Phoenix and everything
written after it was drafted were not.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, StringConstraints
from typing_extensions import Annotated

__all__ = ["ProjectShape", "ProjectShapeUnknown", "shape_persona", "detect_shape", "repo_tree"]

#: How much of the tree to show the model. A repository's own layout is legible
#: from a few hundred paths; the whole tree of a monorepo is not, and is mostly
#: vendored files that say nothing about how the project is run.
MAX_TREE_PATHS = 400

#: Manifests are read in full when small, because their content is what a person
#: actually reads (a package.json's scripts, a Makefile's targets). This is a
#: size bound, not a list of which files count.
MAX_MANIFEST_BYTES = 4000


class ProjectShapeUnknown(RuntimeError):
    """The model could not work out what this repository is.

    QA's posture, now both workflows': a stack nobody can characterise stops the
    card and says why. The alternative is documenting a repository from a blank
    picture, or booting an app with a guessed command and reporting whatever
    comes back -- both of which look like results.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ProjectShape(BaseModel):
    """What this repository is and how it runs, as the model read it."""

    model_config = {"extra": "forbid"}

    #: The project's own name, as its manifest or layout gives it.
    project_name: Annotated[str, StringConstraints(max_length=200)] = ""
    #: What kind of project this is, in the model's words rather than from a
    #: vocabulary coordinare defined. "a Rails API with a React front end" is a
    #: more useful sentence than any enum member would be.
    summary: Annotated[str, StringConstraints(max_length=500)] = ""
    #: How to run the tests, exactly as it would be typed. Empty when the
    #: repository has no test suite the model could find -- which is a fact
    #: about the repository, not a failure to characterise it.
    test_command: Annotated[str, StringConstraints(max_length=300)] = ""
    #: How to start the app so a browser can reach it, exactly as it would be
    #: typed. Empty when this is not a bootable application (a library, a CLI).
    start_command: Annotated[str, StringConstraints(max_length=300)] = ""
    #: How long starting it plausibly takes. Replaces a fixed 60s default that
    #: reasoned about Rails migrations in a comment: the model has just read the
    #: boot path and is better placed to say.
    boot_seconds: int = Field(default=60, ge=1, le=900)
    #: Top-level directories holding the source, largest or most central first.
    #: Replaces a hardcoded skip list and an 18-entry file-extension table.
    source_dirs: list[str] = Field(default_factory=list)
    #: Set ONLY when the model cannot say what this repository is. Anything here
    #: stops the card. Note this is distinct from an empty ``test_command`` or
    #: ``start_command``: "this is a Go library with no server" is a successful
    #: characterisation, and not a stop.
    cannot_determine: Annotated[str, StringConstraints(max_length=500)] = ""


def shape_persona() -> str:
    """Read a repository and say what it is. Names no language and no file."""
    return (
        "You are opening a repository you have never seen and working out what "
        "it is, the way you would on your first day.\n\n"
        "From the layout and the manifest files below, report:\n"
        "- project_name: what the project calls itself\n"
        "- summary: what kind of project this is, in your own words\n"
        "- test_command: exactly what you would type to run its tests\n"
        "- start_command: exactly what you would type to start it so a browser "
        "could reach it on 127.0.0.1 at the port in $PORT\n"
        "- boot_seconds: how long starting it plausibly takes, allowing for "
        "whatever it does on startup\n"
        "- source_dirs: the top-level directories holding the source, most "
        "central first, excluding tests, docs, vendored code and build output\n\n"
        "Leave test_command empty if the repository genuinely has no test "
        "suite, and start_command empty if it is not something that can be "
        "started and browsed -- a library or a command line tool. Those are "
        "answers, not failures.\n\n"
        "Use cannot_determine ONLY when you cannot tell what this repository is "
        "at all. It stops the work and asks a human, which is the right outcome "
        "when the alternative is documenting a project nobody understood or "
        "booting one with a guessed command. Say what you would need to see."
    )


def _manifest_excerpts(workspace: Path, tree: set[str]) -> str:
    """The content of the repository's root files, whatever they happen to be.

    Deliberately not a list of known manifest names. A person opening a
    repository reads what is at the top level, and the interesting file might be
    ``mix.exs``, ``Justfile``, ``deno.json`` or something drafted after this was
    written. Root files are bounded in number and in size, so read them all and
    let the model decide which ones mattered.
    """
    roots = sorted(p for p in tree if "/" not in p)
    parts: list[str] = []
    for name in roots:
        path = workspace / name
        try:
            if not path.is_file() or path.stat().st_size > MAX_MANIFEST_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not text.strip():
            continue
        parts.append(f"--- {name} ---\n{text}")
    return "\n\n".join(parts) or "(no readable root files)"


async def detect_shape(toolkit: Any, workspace: Path, tree: set[str]) -> ProjectShape:
    """Work out what this repository is. One model call.

    Raises:
        ProjectShapeUnknown: the model could not characterise the repository.
    """
    from performer.workflows.budget import Budget

    listing = "\n".join(sorted(tree)[:MAX_TREE_PATHS]) or "(empty repository)"
    more = max(0, len(tree) - MAX_TREE_PATHS)
    content = [{
        "type": "text",
        "text": (
            f"Repository layout ({len(tree)} paths"
            + (f", showing the first {MAX_TREE_PATHS}" if more else "")
            + f"):\n{listing}\n\n"
            f"Root files:\n{_manifest_excerpts(workspace, tree)}\n\n"
            "Return the project shape JSON."
        ),
    }]
    shape = await toolkit.call_model(
        persona=shape_persona(), schema=ProjectShape, content=content,
        budget=Budget.for_step("project_shape"),
    )
    if shape.cannot_determine.strip():
        raise ProjectShapeUnknown(shape.cannot_determine.strip())
    return shape


async def repo_tree(toolkit: Any, workspace: Path) -> set[str]:
    """The repository's tracked paths.

    Shared by the two workflows that ask what this project is, so the reading is
    taken from the same view of the repository in both.
    """
    result = await toolkit.run_command("git ls-files", cwd=workspace, timeout_s=60)
    return {line.strip() for line in (result.output_excerpt or "").splitlines() if line.strip()}
