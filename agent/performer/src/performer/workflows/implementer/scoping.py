"""Running only the tests a milestone is about (#379).

The TDD inner loop asks a question about one milestone's test files -- did they
fail for the right reason, do they pass now. Coordinare answered it by running
the whole suite. On the website symphony that is ~11 minutes a run, three runs
per milestone iteration, against 0.307s for the agent's own scoped run of the
same files in the same session. Three orders of magnitude, for the question the
check actually asks.

The whole suite still runs, once, where the *regression* question belongs: the
spec-089 local gate, which runs it unconditionally before the PR opens and
rejects anything short of a clean pass. (Not the quality phase -- that runs the
lint set, and reaches the suite only inside a repair turn after a quality
command has failed.)

Scoping is runner-specific -- every runner spells "just these files" its own
way, and some cannot do it at all. A table of those spellings is exactly the
hardcoded stack knowledge #364 removed from this codebase, and would re-create
the class of defect #365/#366/#367 just deleted. So the command comes from the
model, the same way ``ProjectShape`` supplies ``test_command`` (#367).

Two ways this fails closed:

* ``can_scope=False`` runs the whole suite. An unsure model must never produce a
  command that quietly selects nothing -- a green check against zero tests is a
  false pass, which is worse than a slow one.
* A returned command that does not mention the files is refused for the same
  reason. That is a check on the *shape* of the answer, not on the runner: it
  asks whether the files appear, and never what the command ought to look like.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, StringConstraints
from typing_extensions import Annotated

__all__ = ["ScopedRun", "scope_persona", "scoped_command"]


class ScopedRun(BaseModel):
    """How this runner is asked for just these test files."""

    model_config = {"extra": "forbid"}

    #: False when the runner cannot select files, or you are not sure it can.
    can_scope: bool
    #: The command to run, set only when ``can_scope``.
    command: Annotated[str, StringConstraints(max_length=1000)] = ""
    #: Why, in terms of the runner you were shown.
    reason: Annotated[str, StringConstraints(max_length=300)] = ""


def scope_persona() -> str:
    """Ask for a narrowed command. Names no runner and no language."""
    return (
        "You are working in an unfamiliar repository. You are shown the command "
        "that runs its whole test suite, and a list of test files that matter "
        "right now.\n\n"
        "Give the command that runs ONLY those files.\n\n"
        "Most test runners accept file paths as arguments, and some need a flag "
        "or a separator before them. Use the form this runner expects, keeping "
        "the rest of the command as given -- the same tool, the same wrapper, "
        "the same options.\n\n"
        "Set can_scope to false if this runner cannot select individual files, "
        "if the command is a wrapper that would ignore the paths, or if you are "
        "not sure. The whole suite runs in that case, which is slow but "
        "correct. A command that appears to select these files but actually "
        "selects nothing is far worse: the tests would be reported as passing "
        "without ever running."
    )


def _names_every_file(command: str, files: Sequence[str]) -> bool:
    """Whether *command* mentions EVERY one of *files*.

    A shape check on the model's answer, not knowledge of any runner: a command
    that omits a requested file cannot be running that file, whatever tool it
    invokes. Callers treat a miss as "do not scope", so the cost of this being
    conservative is a whole-suite run.

    Every, not any. A command naming only some of the milestone's test files
    passes an "any" check while silently never running the rest, and the run
    then reports green because the dropped tests produced no failures -- the
    false green this module exists to prevent, reached through the guard meant
    to prevent it.
    """
    return all(path in command for path in files if path)


async def scoped_command(toolkit: Any, base_command: str, files: Sequence[str]) -> str | None:
    """The command that runs only *files*, or None to run the whole suite."""
    from performer.workflows.budget import Budget

    paths = [p for p in files if p]
    if not paths:
        return None

    listing = "\n".join(f"- {p}" for p in paths[:100])
    content = [{
        "type": "text",
        "text": (
            f"Whole-suite command: {base_command}\n\n"
            f"Test files to run:\n{listing}\n\n"
            "Return the scoped-run JSON."
        ),
    }]
    answer = await toolkit.call_model(
        persona=scope_persona(), schema=ScopedRun, content=content,
        budget=Budget.for_step("implementer_scope_tests"),
    )
    command = (answer.command or "").strip()
    if not answer.can_scope or not command:
        return None
    if not _names_every_file(command, paths):
        return None
    return command
