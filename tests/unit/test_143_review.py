"""#143 review round: unit checks for blocks 5 and the descriptor, label and leave-guard items.

Each test pins one reviewed defect and fails on the review's build.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from pydantic import BaseModel, SecretStr

from coordinare import config_descriptors as cd
from coordinare.config_descriptors import SECRET_MASK
from coordinare.dashboard import _DASHBOARD_JS_SOURCES
from tests.unit.dashboard_js_slices import shipped_function
from tests.unit.test_143_settings import _PERFORMERS, _config

_NODE = shutil.which("node")


class _Synthetic(BaseModel):
    """A field the name list cannot know about: only its type says it is a secret."""

    vendor_credential: SecretStr
    optional_credential: SecretStr | None = None
    plain_name: str = "visible"


_RAW = "sk-LIVE-123456"


# --- Block 5: secrets are detected by type, not only by name -------------------


def test_an_unannotated_secretstr_field_is_masked_and_typed_as_a_secret() -> None:
    field = _Synthetic.model_fields["vendor_credential"]
    setting = cd.setting_from_field("global", "vendor_credential", field, SecretStr(_RAW))
    assert setting.secret is True
    assert setting.type == "secret"
    assert setting.current_value == SECRET_MASK
    assert _RAW not in json.dumps(setting.model_dump(mode="json"))


def test_an_optional_secretstr_field_is_masked_too() -> None:
    field = _Synthetic.model_fields["optional_credential"]
    setting = cd.setting_from_field("global", "optional_credential", field, SecretStr(_RAW))
    assert setting.secret is True
    assert setting.current_value == SECRET_MASK
    assert _RAW not in json.dumps(setting.model_dump(mode="json"))


def test_an_unset_optional_secret_is_not_shown_as_masked_or_as_a_value() -> None:
    field = _Synthetic.model_fields["optional_credential"]
    setting = cd.setting_from_field("global", "optional_credential", field, None)
    assert setting.secret is True
    assert setting.current_value is None


def test_a_plain_string_field_is_still_not_a_secret() -> None:
    field = _Synthetic.model_fields["plain_name"]
    setting = cd.setting_from_field("global", "plain_name", field, "visible")
    assert setting.secret is False
    assert setting.current_value == "visible"


# --- log_level and output_mode keep their dropdowns ----------------------------


def _global_settings() -> dict[str, Any]:
    snapshot = cd.build_snapshot(_config(_PERFORMERS))
    section = next(sec for sec in snapshot.sections if sec.id == "global")
    return {setting.key.split(".")[-1]: setting for setting in section.settings}


def test_log_level_is_a_dropdown_with_its_allowed_values() -> None:
    setting = _global_settings()["log_level"]
    assert setting.type == "enum"
    assert setting.enum == ["debug", "info", "warning", "error"]


def test_output_mode_is_a_dropdown_with_its_allowed_values() -> None:
    setting = _global_settings()["output_mode"]
    assert setting.type == "enum"
    assert setting.enum == ["human", "structured"]


# --- the Used-by label escapes exactly once (node) -----------------------------


def _run_node(tmp_path: Path, body: str) -> None:
    assert _NODE is not None, "node is required for the dashboard JS gates"
    script = tmp_path / "check.js"
    script.write_text(
        "\n\n".join([
            _DASHBOARD_JS_SOURCES["vocabulary"],
            shipped_function("helpers", "esc"),
            shipped_function("helpers", "vocabularyKindLabel"),
            shipped_function("config", "cfgUsedByLabel"),
            shipped_function("config", "cfgUsedByHtml"),
            shipped_function("config", "cfgLeaveAllowed"),
            "var CFG_HINT = '';",
            body,
            "console.log('ok');",
        ]),
        encoding="utf-8",
    )
    result = subprocess.run([_NODE, str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_stage_label_with_an_ampersand_is_escaped_once(tmp_path: Path) -> None:
    _run_node(tmp_path, """
COORDINARE_VOCABULARY.stage_labels = {implementing: 'Build & ship'};
const html = cfgUsedByHtml([{kind: 'stage', name: 'implementing'}]);
if (html.indexOf('Build &amp; ship stage') === -1) throw new Error('not escaped once: ' + html);
if (html.indexOf('&amp;amp;') !== -1) throw new Error('double escaped: ' + html);
""")


# --- leaving with unsaved edits matches the page path exactly (node) -----------


def test_a_path_that_only_starts_with_config_does_not_skip_the_prompt(tmp_path: Path) -> None:
    _run_node(tmp_path, """
var window = globalThis;
let asked = 0;
window.confirm = function() { asked += 1; return false; };
cfgHasUnsaved = function() { return true; };
if (cfgLeaveAllowed('/configuration') !== false) throw new Error('/configuration skipped the prompt');
if (asked !== 1) throw new Error('prompt not shown for /configuration');
if (cfgLeaveAllowed('/config') !== true) throw new Error('/config itself must stay allowed');
""")


# --- the review's uses are wired as data, not as strings in onclick ------------


def test_no_inline_onclick_is_built_from_config_data() -> None:
    """XSS: data reaches handlers through data-* attributes and one listener, never a quoted JS string."""
    source = _DASHBOARD_JS_SOURCES["config"]
    assert "onclick=\"cfgSaveItem(this, &quot;' + cat" not in source
    assert "cfgSaveSection(this, &quot;" not in source
    assert "cfgDeleteItem(this, &quot;" not in source
    assert "cfgCreateItem(this, &quot;" not in source
    assert "cfgSaveRoutingEntry(this," not in source
    assert "navigate(event,&quot;" not in source
