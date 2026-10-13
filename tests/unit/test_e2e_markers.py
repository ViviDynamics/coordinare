"""Every browser test must carry the ``e2e`` marker.

The default run (``-m 'not e2e'``) has no Playwright browser. A test under
``tests/e2e`` that loses its marker (a merge conflict resolved carelessly did
exactly that) is collected by the unit job and fails there with "Executable
doesn't exist". This catches it locally, in the suite that would otherwise
break.
"""
from __future__ import annotations

import ast
from pathlib import Path

_E2E_DIR = Path(__file__).resolve().parents[1] / "e2e"


def _is_e2e_marker(node: ast.expr) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    return (
        isinstance(target, ast.Attribute)
        and target.attr == "e2e"
        and isinstance(target.value, ast.Attribute)
        and target.value.attr == "mark"
    )


def _module_is_marked(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "pytestmark" for target in node.targets
        ):
            values = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
            return any(_is_e2e_marker(value) for value in values)
    return False


def test_every_browser_test_is_marked_e2e() -> None:
    unmarked: list[str] = []
    files = sorted(_E2E_DIR.glob("test_*.py"))
    assert files, "no browser test modules were found"
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if _module_is_marked(tree):
            continue
        for node in tree.body:
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")
                and not any(_is_e2e_marker(decorator) for decorator in node.decorator_list)
            ):
                unmarked.append(f"{path.name}::{node.name}")
    assert not unmarked, f"browser tests without @pytest.mark.e2e: {unmarked}"
