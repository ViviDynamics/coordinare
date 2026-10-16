from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from playwright.sync_api import BrowserContext

from tests.e2e import conftest as e2e_fixtures

_E2E_DIR = Path(__file__).resolve().parents[1] / "e2e"
_EXPECTED_NAVIGATION_TIMEOUT_MS = 15_000


def _navigation_timeout_overrides(source: str) -> list[int]:
    """Find explicit navigation timeouts, including named and multiline values."""
    return sorted(
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"goto", "reload"}
        and any(keyword.arg == "timeout" for keyword in node.keywords)
    )


@pytest.mark.parametrize(
    ("source", "expected_lines"),
    [
        ('page.goto(url, timeout=3_000)', [1]),
        ('page.reload(timeout=_WAIT_SSE)', [1]),
        ('page.goto(\n    url,\n    timeout=WAIT_PAGE,\n)', [1]),
        ('page.goto(url, wait_until="domcontentloaded")\npage.reload()', []),
        ('expect(page.locator("#phase")).to_have_text("Idle", timeout=3_000)', []),
    ],
)
def test_navigation_guard_distinguishes_navigation_from_assertion_waits(
    source: str, expected_lines: list[int],
) -> None:
    assert _navigation_timeout_overrides(source) == expected_lines


def test_e2e_navigation_inherits_the_shared_timeout() -> None:
    files = sorted(_E2E_DIR.rglob("*.py"))
    assert files, "no browser test modules were found"
    overrides = [
        f"{path.relative_to(_E2E_DIR)}:{line}"
        for path in files
        for line in _navigation_timeout_overrides(path.read_text(encoding="utf-8"))
    ]
    assert not overrides, f"navigation must inherit the shared timeout: {overrides}"


def test_shared_context_sets_only_the_navigation_timeout() -> None:
    browser_context = create_autospec(BrowserContext, instance=True)

    configured = e2e_fixtures.context.__wrapped__(browser_context)

    assert configured is browser_context
    browser_context.set_default_navigation_timeout.assert_called_once_with(_EXPECTED_NAVIGATION_TIMEOUT_MS)
    browser_context.set_default_timeout.assert_not_called()
