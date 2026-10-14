"""#139 review (PR #210): user-facing wording and the switcher's scoping rules.

Each section pins one review item. The browser halves live in
tests/e2e/test_139_symphony_switcher.py.
"""
from __future__ import annotations

import re
from pathlib import Path

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
