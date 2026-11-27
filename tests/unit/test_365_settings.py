"""#365 (d) and (e): the approver setting says the rule, and the logins field reads unambiguously.

Also the Settings page's "Saved" message, which the one-second refresh after a save used to
wipe (noted in #411). The pure JavaScript runs under node (tests/js/settings_365_checks.js);
the browser flow is in tests/e2e/test_365_settings.py.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from coordinare import config_descriptors as cd
from coordinare.config import ProjectConfiguration
from tests.unit.test_221_symphony_editor_js import _function

_ROOT = Path(__file__).resolve().parents[2]
_STATIC = _ROOT / "src" / "coordinare" / "dashboard" / "dashboard_static"
_CHECKS = _ROOT / "tests" / "js" / "settings_365_checks.js"


def _setting(key: str, value: object) -> cd.ConfigSetting:
    return cd.setting_from_field("global", key, ProjectConfiguration.model_fields[key], value)


def test_the_approver_field_is_labelled_for_what_it_decides() -> None:
    setting = _setting("human_reviewers", ["alice"])
    assert setting.label == "Who can approve merges (human_reviewers)"
    assert setting.key.endswith("human_reviewers")


def test_the_approver_help_states_the_rule_exactly() -> None:
    help_text = _setting("human_reviewers", ["alice"]).help or ""
    assert "Only these GitHub logins can approve a merge" in help_text
    assert "every symphony" in help_text
    assert "does not merge" in help_text
    assert "work item" in help_text
    assert chr(0x2014) not in help_text and chr(0x2013) not in help_text  # no em or en dash


def test_the_bot_field_says_a_bot_cannot_approve_a_merge() -> None:
    setting = _setting("trusted_bot_reviewers", [])
    assert setting.label == "Bots whose reviews are acted on (trusted_bot_reviewers)"
    assert "never approves a merge" in (setting.help or "")


def test_the_settings_functions_pass_their_checks_under_node(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    config = (_STATIC / "config.js").read_text(encoding="utf-8")
    app = (_STATIC / "app.js").read_text(encoding="utf-8")
    helpers = (_STATIC / "helpers.js").read_text(encoding="utf-8")
    start = config.index("var CFG_LOGIN_FIELDS")
    shipped = [
        "var _CFG_REFS = {}; var CFG_INPUT = ''; var CFG_HINT = '';",
        config[start:config.index(";", start) + 1],
        _function(helpers, "esc"),
        *(_function(config, name) for name in (
            "cfgParseLogins", "cfgLoginsPreviewHtml", "cfgLoginsErrors", "cfgInput", "cfgCoerce",
            "cfgSavedNotice", "cfgRestoreNotice",
        )),
        _function(app, "symApproverNoteHtml"),
    ]
    script = tmp_path / "settings_365.js"
    script.write_text("\n\n".join([*shipped, _CHECKS.read_text(encoding="utf-8")]), encoding="utf-8")
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SUMMARY ok" in result.stdout


def test_the_settings_save_hands_its_message_to_the_refreshed_page() -> None:
    config = (_STATIC / "config.js").read_text(encoding="utf-8")
    handler = _function(config, "cfgHandleSave")
    assert "cfgSavedNotice(" in handler
    assert "loadConfigPage(" in handler
    loader = _function(config, "loadConfigPage")
    assert "cfgRestoreNotice(" in loader


def test_a_flagged_login_stops_the_save_before_the_review() -> None:
    config = (_STATIC / "config.js").read_text(encoding="utf-8")
    save = _function(config, "cfgSaveSection")
    assert save.index("cfgLoginsErrors(") < save.index("cfgShowReview(")


def test_the_symphony_form_carries_the_approver_note() -> None:
    app = (_STATIC / "app.js").read_text(encoding="utf-8")
    assert "symApproverNoteHtml(" in _function(app, "symFormHtml")
