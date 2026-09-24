"""436: dashboard.py decomposes into a package of per-area routers + services.

Acceptance criteria from the issue:

- ``coordinare.dashboard`` is a thin package root, not a 5,577-line module.
- UI strings live in template files, not inline Python string constants.
- Per-area router modules, each under ~800 lines.
- No function anywhere under the dashboard package exceeds 100 lines
  (create_dashboard_app was ~1,835, build_snapshot ~330, update_global_config
  ~156, _version_refusal ~115, create_symphony ~108).
- The package is off the mypy strict allowlist (modules type-clean).
- Every public and test-visible name keeps importing from
  ``coordinare.dashboard`` unchanged, so no test or caller edits are needed.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

PACKAGE = "coordinare.dashboard"

EXPECTED_IMPORT_SURFACE = [
    "DASHBOARD_HTML_BUDGET_BYTES",
    "DashboardStore",
    "SESSION_EVENT_LIMIT",
    "SSEBroadcaster",
    "_ACTIVITY_STREAM_JS",
    "_ASSISTANT_FRAGMENT",
    "_DASHBOARD_HTML",
    "_DASHBOARD_JS_FILES",
    "_DASHBOARD_JS_REVISIONS",
    "_DASHBOARD_JS_SOURCES",
    "_entity_tags",
    "_json_default",
    "asyncio",
    "check_port_available",
    "compute_overall_health",
    "create_dashboard_app",
    "format_phase_label",
    "is_session_stale",
    "ownership_hint",
    "register_webhook_route",
    "render_performer_pool_widget",
    "verify_github_signature",
]


def dashboard_root() -> Path:
    module = importlib.import_module(PACKAGE)
    assert module.__file__ is not None
    return Path(module.__file__).resolve().parent


def package_python_files() -> list[Path]:
    root = dashboard_root()
    return sorted(root.rglob("*.py"))


def test_dashboard_is_a_package_with_split_modules() -> None:
    root = dashboard_root()
    assert root.name == "dashboard"
    assert (root / "__init__.py").is_file()
    assert (root / "routers").is_dir()
    assert (root / "templates").is_dir()
    template_names = {p.name for p in (root / "templates").iterdir()}
    assert "dashboard.html" in template_names
    assert "assistant_fragment.html" in template_names
    assert "activity_streams.js" in template_names


def test_no_function_exceeds_100_lines() -> None:
    offenders: list[str] = []
    for path in package_python_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                length = (node.end_lineno or node.lineno) - node.lineno + 1
                if length > 100:
                    offenders.append(
                        f"{path.name}:{node.lineno} {node.name} spans {length} lines",
                    )
    assert not offenders, "functions over 100 lines:\n" + "\n".join(offenders)


def test_router_modules_stay_under_800_lines() -> None:
    root = dashboard_root()
    routers_dir = root / "routers"
    assert routers_dir.is_dir(), "per-area routers must live in routers/"
    for path in sorted(routers_dir.glob("*.py")):
        line_count = len(path.read_text().splitlines())
        assert line_count <= 800, (
            f"{path.name} is {line_count} lines; per-router budget is ~800"
        )


def test_dashboard_off_mypy_allowlist() -> None:
    pyproject = dashboard_root().parents[2] / "pyproject.toml"
    text = pyproject.read_text()
    assert 'module = "coordinare.dashboard"' not in text, (
        "coordinare.dashboard must be type-clean and off the strict allowlist"
    )


def test_public_import_surface_preserved() -> None:
    module = importlib.import_module(PACKAGE)
    missing = [n for n in EXPECTED_IMPORT_SURFACE if not hasattr(module, n)]
    assert not missing, f"missing from coordinare.dashboard: {missing}"


def test_asyncio_patch_target_resolves() -> None:
    """Tests patch ``coordinare.dashboard.asyncio.wait_for`` — the attribute
    must exist as a module attribute (import asyncio at package root)."""

    module = importlib.import_module(PACKAGE)
    assert module.asyncio is importlib.import_module("asyncio")


def _expected_template_bytes(path: Path) -> int:
    return path.stat().st_size


class TestTemplatesAreAuthoritative:
    """The inline constants must come from the template files."""

    def test_dashboard_html_spliced_from_template_file(self) -> None:
        root = dashboard_root()
        template = root / "templates" / "dashboard.html"
        module = importlib.import_module(PACKAGE)
        raw = template.read_text(encoding="utf-8")
        assert "<!--349-static-js-->" in raw
        head, tail = raw.split("<!--349-static-js-->")
        assert module._DASHBOARD_HTML.startswith(head)
        assert module._DASHBOARD_HTML.endswith(tail)

    def test_assistant_fragment_served_from_template_file(self) -> None:
        root = dashboard_root()
        template = root / "templates" / "assistant_fragment.html"
        module = importlib.import_module(PACKAGE)
        assert template.read_text(encoding="utf-8") == module._ASSISTANT_FRAGMENT
