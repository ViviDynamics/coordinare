"""#139 review (PR #210): user-facing wording and the switcher's scoping rules.

Each section pins one review item. The browser halves live in
tests/e2e/test_139_symphony_switcher.py.
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

from coordinare.dashboard import _DASHBOARD_JS_SOURCES
from coordinare.dashboard.assets import _DASHBOARD_JS_DIR, _TEMPLATE_DIR
from tests.unit.dashboard_js_slices import shipped_function

_SRC = Path(__file__).resolve().parents[2] / "src" / "coordinare"

# --- item 3: no "Board" in anything a user reads -------------------------------- #


def test_user_facing_protocol_descriptions_name_cards_and_github_project_columns() -> None:
    protocol = (_SRC / "protocol.py").read_text(encoding="utf-8")
    assert not re.search(r"\bBoard\b", protocol), "protocol.py still says Board"
    assert "GitHub Project column" in protocol


def test_the_paused_card_activity_and_error_name_the_github_project_not_the_board() -> None:
    daemon = (_SRC / "daemon.py").read_text(encoding="utf-8")
    assert "Board paused" not in daemon
    assert "The card moved out of its GitHub Project column" in daemon


# --- item 1: every top-level snapshot field declares how a symphony scopes it --------- #

_NODE_SNAPSHOT_SCOPE_CHECKS = Path(__file__).resolve().parents[1] / "js" / "snapshot_scope_checks.js"
_NODE_LAST_STATE_CHECKS = Path(__file__).resolve().parents[1] / "js" / "last_state_scope_checks.js"

# The builders whose returned keys make up the top-level snapshot, and the ones that build a session row.
_SNAPSHOT_BUILDERS = {"build_snapshot", "_snapshot_core", "_snapshot_extras", "_session_summaries"}
_SESSION_BUILDERS = {"_session_summary", "_top_state_summary"}


def _keys_built_by(names: set[str]) -> list[str]:
    """Every key the named builders return, or assign with ``x["key"] = ...``, read from store.py's AST.

    A key added to a builder's returned dict, to a dict merged into the snapshot, or set by item
    assignment appears here, so nobody can forget to declare its scope.
    """
    tree = ast.parse((_SRC / "dashboard" / "store.py").read_text(encoding="utf-8"))
    keys: set[str] = set()
    for func in ast.walk(tree):
        if not (isinstance(func, ast.FunctionDef) and func.name in names):
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
                keys.update(
                    key.value for key in node.value.keys
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                )
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and isinstance(target.slice.value, str)
                    ):
                        keys.add(target.slice.value)
    return sorted(keys)


def test_every_snapshot_key_declares_its_scope_and_every_session_key_is_declared(tmp_path: Path) -> None:
    keys = _keys_built_by(_SNAPSHOT_BUILDERS)
    session_keys = _keys_built_by(_SESSION_BUILDERS)
    assert len(keys) > 30, "the AST walk found too few snapshot keys"
    assert len(session_keys) > 15, "the AST walk found too few session-row keys"
    keys_file = tmp_path / "keys.json"
    keys_file.write_text(json.dumps(keys), encoding="utf-8")
    session_file = tmp_path / "session-keys.json"
    session_file.write_text(json.dumps(session_keys), encoding="utf-8")
    scope_js = tmp_path / "symphonies.js"
    scope_js.write_text(_DASHBOARD_JS_SOURCES["symphonies"], encoding="utf-8")
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    result = subprocess.run(
        [node, str(_NODE_SNAPSHOT_SCOPE_CHECKS), str(scope_js), str(keys_file), str(session_file)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"snapshot scope checks failed:\n{report}"


def test_every_read_of_the_full_snapshot_goes_through_the_scope_or_is_allow_listed() -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    result = subprocess.run(
        [node, str(_NODE_LAST_STATE_CHECKS), str(_DASHBOARD_JS_DIR / "symphonies.js"),
         str(_DASHBOARD_JS_DIR), str(_TEMPLATE_DIR)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"a read of _lastState bypasses the scope:\n{report}"


def test_the_unclaimed_label_names_the_github_project_and_never_says_board() -> None:
    label = shipped_function("attention", "attentionDetail")
    assert "not on any symphony's GitHub Project" in label
    assert not re.search(r"board", label, re.IGNORECASE), label


# --- item 7: per-symphony spend reconciles with the total ------------------------ #


def _usage_record(seq: int, tokens: int) -> dict:
    return {
        "ts": "2026-10-14T10:00:00+00:00", "seq": seq, "type": "usage", "scope": "message",
        "usage": {"input_tokens": tokens, "output_tokens": 0, "cache_read": 0, "cache_write": 0,
                  "model": "claude-opus-4-5"},
    }


def _opus_table():
    from coordinare.config import CostTrackingConfig
    from coordinare.services.token_usage import price_table_from_config

    return price_table_from_config(CostTrackingConfig.model_validate({
        "pricing_as_of": "2026-10-01",
        "prices": [{"models": ["claude-opus-4-5"], "input_per_million": 15.0, "output_per_million": 75.0,
                    "cache_read_per_million": 1.5, "cache_write_per_million": 18.75}],
    }))


def test_the_symphony_parts_and_the_unattributed_remainder_sum_to_the_total() -> None:
    from datetime import date

    from coordinare.services.token_usage import TokenLedger, usage_block

    ledger = TokenLedger()
    ledger.observe("r1", symphony="alpha", card="C1", stage="implementing", records=[_usage_record(1, 10)])
    ledger.observe("r2", symphony="beta", card="C2", stage="implementing", records=[_usage_record(1, 20)])
    ledger.observe("r3", symphony="", card="C3", stage="implementing", records=[_usage_record(1, 40)])
    block = usage_block(ledger, _opus_table(), today=date(2026, 10, 14), symphonies=["alpha", "beta"])

    parts = sum(block["by_symphony"][name]["today"]["tokens"]["input"] for name in ("alpha", "beta"))
    remainder = block["unattributed"]["today"]["tokens"]["input"]
    assert parts == 30
    assert remainder == 40
    assert parts + remainder == block["today"]["tokens"]["input"] == 70


def test_a_symphony_with_no_runs_has_zero_figures_not_a_missing_key() -> None:
    from datetime import date

    from coordinare.services.token_usage import TokenLedger, usage_block

    block = usage_block(TokenLedger(), _opus_table(), today=date(2026, 10, 14), symphonies=["quiet"])
    assert block["by_symphony"]["quiet"]["today"]["tokens"]["input"] == 0


# --- item 7: a symphony's repository follows effective_config for an empty override --- #


def test_an_empty_project_name_override_means_the_symphony_name_not_the_global_repo() -> None:
    from types import SimpleNamespace

    from coordinare.dashboard.store import DashboardStore

    global_cfg = SimpleNamespace(github_org="acme", project_name="global-repo")
    sym_cfg = SimpleNamespace(overrides={"project_name": ""})
    assert DashboardStore._symphony_repository("sample", sym_cfg, global_cfg) == ("acme", "sample")


def test_an_empty_github_org_override_is_kept_as_effective_config_keeps_it() -> None:
    from types import SimpleNamespace

    from coordinare.dashboard.store import DashboardStore

    global_cfg = SimpleNamespace(github_org="acme", project_name="repo")
    sym_cfg = SimpleNamespace(overrides={"github_org": ""})
    assert DashboardStore._symphony_repository("sample", sym_cfg, global_cfg) == ("", "repo")
