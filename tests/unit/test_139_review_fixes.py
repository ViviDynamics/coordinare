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
