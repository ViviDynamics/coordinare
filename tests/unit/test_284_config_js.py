from __future__ import annotations

import subprocess
from pathlib import Path

from tests.unit.test_221_symphony_editor_js import _function

_ROOT = Path(__file__).resolve().parents[2]


def test_seed_banner_escapes_values_and_shows_reset_instruction(tmp_path: Path) -> None:
    source = (_ROOT / 'src/coordinare/dashboard/dashboard_static/config.js').read_text()
    script = tmp_path / 'config_284.js'
    declarations = source[source.index('var _cfgChartPreview ='):source.index('function cfgChartDiffHtml(')]
    script.write_text(declarations + '\n'.join(_function(source, name) for name in (
        'cfgSeedBanner', 'cfgChartDiffHtml', 'cfgReviewChart', 'cfgConfirmChart',
    )) + '\n' + (_ROOT / 'tests/js/config_284_checks.js').read_text())
    result = subprocess.run(['node', str(script)], capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'SUMMARY ok' in result.stdout
