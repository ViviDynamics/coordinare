"""412 round 38: drift guard — the coordinare transport's process-exiting
status set must stay exactly equal to the statuses on which the performer
run loop actually breaks.

The two live in separate packages (coordinare keeps a literal to avoid a
runtime import of the performer package in the daemon). The set's meaning
is "the performer process has exited, so the transport may drop its
reference": a status in the set that the loop does not exit on would drop a
live multi-turn session, and a loop-exiting status missing from the set
would idle a dead process until the reap's SIGTERM. This test is the
mechanical link between the mirror and the loop.
"""
from __future__ import annotations

import ast
from pathlib import Path

from coordinare.protocol import PROCESS_EXITING_STATUSES


def _run_loop_break_statuses() -> set[str]:
    """Extract the status tuple behind the run loop's only break-on-response.

    438: the loop exits via `if _loop_should_exit(resp, msg, perf): break`, and
    the status tuple lives in that helper, so resolve break -> helper -> tuple.
    """
    main_path = Path(__file__).parents[2] / "agent" / "performer" / "src" / "performer" / "main.py"
    tree = ast.parse(main_path.read_text(encoding="utf-8"))
    statuses: set[str] | None = None
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.If)
            and len(node.body) == 1
            and isinstance(node.body[0], ast.Break)
            and isinstance(node.test, ast.Call)
            and isinstance(node.test.func, ast.Name)
            and node.test.func.id == "_loop_should_exit"
        ):
            continue
        helper = next(
            (
                fn
                for fn in tree.body
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == "_loop_should_exit"
            ),
            None,
        )
        assert helper is not None, "_loop_should_exit must stay a module-level function in main.py"
        for sub in ast.walk(helper):
            if isinstance(sub, (ast.Tuple, ast.List)):
                candidate = {element.value for element in sub.elts if isinstance(element, ast.Constant)}
                if "pr_opened" in candidate and "blocked" in candidate and "nothing_to_review" in candidate:
                    statuses = candidate
    assert statuses is not None, "the run loop's break-on-status statement was not found in main.py"
    return statuses


def test_process_exiting_statuses_mirror_the_run_loop_break_tuple() -> None:
    """The set is the break tuple plus one documented superset entry:
    `session_expired` exits the loop only when no session is active, and the
    transport cannot see session state, so it clears the process there too.
    Anything else in either direction is drift."""
    statuses = _run_loop_break_statuses()
    assert statuses <= set(PROCESS_EXITING_STATUSES), (
        "run-loop break statuses missing from PROCESS_EXITING_STATUSES: "
        f"{sorted(statuses - set(PROCESS_EXITING_STATUSES))}"
    )
    extra = set(PROCESS_EXITING_STATUSES) - statuses
    assert extra == {"session_expired"}, (
        f"PROCESS_EXITING_STATUSES has statuses the loop never breaks on: {sorted(extra)}"
    )
