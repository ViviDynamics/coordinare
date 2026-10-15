"""266: the env bootstrap's browser words, checked as shipped.

The pure functions run under node (tests/js/bootstrap_266_checks.js) on the bundle the dashboard serves:
helpers, usage, symphonies, layout, waiting and the activity stream labels. The static checks read the
shipped scripts and the page: the page stays under its byte budget, and no shipped line starts with the
word the quality guard rejects.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from coordinare.dashboard import _DASHBOARD_HTML, _DASHBOARD_JS_SOURCES
from coordinare.dashboard.assets import DASHBOARD_HTML_BUDGET_BYTES

_REPO = Path(__file__).resolve().parents[2]
_ACTIVITY_STREAMS = (_REPO / "src" / "coordinare" / "dashboard" / "templates" / "activity_streams.js").read_text(
    encoding="utf-8",
)
_BUNDLE_NAMES = ("helpers", "usage", "symphonies", "layout", "waiting")


def test_the_bootstrap_functions_pass_their_node_checks(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    bundle = tmp_path / "bootstrap_266_bundle.js"
    bundle.write_text(
        "\n\n".join([*(_DASHBOARD_JS_SOURCES[name] for name in _BUNDLE_NAMES), _ACTIVITY_STREAMS]),
        encoding="utf-8",
    )
    checks = _REPO / "tests" / "js" / "bootstrap_266_checks.js"
    result = subprocess.run(
        [node, str(checks), str(bundle)], capture_output=True, text=True, timeout=60, check=False,
    )
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"bootstrap checks failed:\n{report}"
    assert "bootstrap_266_checks:" in result.stdout, report


def test_the_dashboard_page_stays_under_its_byte_budget() -> None:
    assert len(_DASHBOARD_HTML.encode("utf-8")) <= DASHBOARD_HTML_BUDGET_BYTES


def test_bootstrap_run_page_node_checks() -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    result = subprocess.run([node, str(_REPO / "tests/js/bootstrap_runs_checks.js")],
                            cwd=_REPO, capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_shipped_line_starts_with_the_word_the_quality_guard_rejects() -> None:
    pattern = re.compile(r"^\s*pending\b")
    for name in _BUNDLE_NAMES:
        for line in _DASHBOARD_JS_SOURCES[name].splitlines():
            assert pattern.match(line) is None, f"{name}.js: {line!r}"
