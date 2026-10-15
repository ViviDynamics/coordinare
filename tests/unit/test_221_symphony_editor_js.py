"""#221: the symphony form's pure functions, run under node exactly as the dashboard ships them.

The functions are read out of config.js and app.js (not copied), so the check cannot drift from the
code the browser runs. tests/js/symphony_221_checks.js holds the assertions.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_STATIC = Path(__file__).resolve().parents[2] / "src" / "coordinare" / "dashboard" / "dashboard_static"
_CHECKS = Path(__file__).resolve().parents[1] / "js" / "symphony_221_checks.js"


def _function(source: str, name: str) -> str:
    """The source of one top-level function, by brace matching from its declaration."""
    start = source.index(f"function {name}(")
    depth = 0
    opened = False
    for index in range(source.index("{", start), len(source)):
        char = source[index]
        if char == "{":
            depth += 1
            opened = True
        elif char == "}":
            depth -= 1
            if opened and depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"{name} has no end")


def test_the_symphony_form_functions_pass_their_checks_under_node(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    config = (_STATIC / "config.js").read_text(encoding="utf-8")
    app = (_STATIC / "app.js").read_text(encoding="utf-8")
    shipped = [
        "var _cfgSymReload = null;",
        *(_function(config, name) for name in (
            "cfgDiffText", "cfgDiffRows", "cfgSecretChanged", "cfgSymphonyRefusal",
            "cfgHandleSymphonySave", "cfgReloadSymphony",
        )),
        _function(app, "symCopy"),
        _function(app, "symRequestBody"),
    ]
    script = tmp_path / "symphony_221.js"
    script.write_text("\n\n".join([*shipped, _CHECKS.read_text(encoding="utf-8")]), encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SUMMARY ok" in result.stdout
