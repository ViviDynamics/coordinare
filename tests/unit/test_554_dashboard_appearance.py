"""Exercise appearance persistence and contrast against the shipped dashboard."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinare.dashboard import _DASHBOARD_HTML
from tests.unit.dashboard_js_slices import page_source
from tests.unit.test_138_contrast import _ev_pairings, contrast_ratio


def test_appearance_resolves_before_external_scripts_and_survives_browser_changes(tmp_path: Path) -> None:
    """Catch late bootstrap, ignored saved choices, and OS overriding explicit choices."""
    boot = re.search(r'<script data-appearance-bootstrap>(.*?)</script>', _DASHBOARD_HTML, re.DOTALL)
    assert boot is not None, "appearance must resolve in the document head"
    assert boot.start() < _DASHBOARD_HTML.index('<script src=')
    script = tmp_path / 'appearance.js'
    script.write_text(boot.group(1))
    node = shutil.which('node')
    assert node, "Node is required for the dashboard behavior checks"
    result = subprocess.run(
        [node, str(Path(__file__).parents[1] / 'js/dashboard_appearance_checks.js'), str(script)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _theme_tokens(theme: str) -> dict[str, str]:
    root = re.search(r':root\s*\{([^}]+)\}', _DASHBOARD_HTML)
    assert root
    css = root.group(1)
    if theme == 'dark':
        dark = re.search(r'html\[data-theme="dark"\]\s*\{([^}]+)\}', _DASHBOARD_HTML)
        assert dark, "dark appearance needs semantic colors"
        css += dark.group(1)
    raw = dict(re.findall(r'(--color-[\w-]+)\s*:\s*([^;]+);', css))
    result = {}
    for name, value in raw.items():
        value = value.strip()
        for _ in range(4):
            alias = re.fullmatch(r'var\((--color-[\w-]+)\)', value)
            if not alias:
                break
            value = raw[alias.group(1)].strip()
        result[name] = value
    return result


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_text_status_and_focus_remain_readable_in_each_theme(theme: str) -> None:
    """Catch palette edits that make content or focus disappear on any surface."""
    tokens = _theme_tokens(theme)
    pairs = list(_ev_pairings().values())
    for surface in ['base', 'surface', 'elevated']:
        for text in ['primary', 'muted']:
            pairs.append((f'--color-bg-{surface}', f'--color-text-{text}'))
    for bg, fg in pairs:
        assert contrast_ratio(tokens[bg], tokens[fg]) >= 4.5, f'{theme}: {fg} on {bg}'
    for surface in ['base', 'surface', 'elevated', 'pill']:
        assert contrast_ratio(tokens[f'--color-bg-{surface}'], tokens['--color-brass-ink']) >= 4.5
    for surface in ['base', 'surface', 'elevated']:
        assert contrast_ratio(tokens[f'--color-bg-{surface}'], tokens['--color-focus']) >= 3


def test_workflow_uses_current_appearance_colors(tmp_path: Path) -> None:
    """Catch a workflow that retains its dark hardcoded colors after a theme switch."""
    from coordinare.dashboard import _DASHBOARD_JS_SOURCES

    assert 'workflow' in _DASHBOARD_JS_SOURCES
    script = tmp_path / 'workflow.js'
    script.write_text(_DASHBOARD_JS_SOURCES['workflow'])
    node = shutil.which('node')
    assert node
    result = subprocess.run(
        [node, '-e', '''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
let surface = '#fffefb';
const ctx = {document: {documentElement: {}},
  getComputedStyle: () => ({getPropertyValue: key => key === '--color-bg-elevated' ? surface : '#214e3d'}),
  window: {addEventListener: () => {}}, mermaid: {initialize: () => {}}};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
assert.ok(ctx.buildFlowDef('idle').includes('fill:#fffefb'));
surface = '#285642';
assert.ok(ctx.buildFlowDef('idle').includes('fill:#285642'));
''', str(script)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_enabled_assistant_remains_inside_the_shared_workspace() -> None:
    """Catch an enabled assistant rendered behind the fixed desktop navigation."""
    from html.parser import HTMLParser

    from coordinare.dashboard.assets import _page_html

    class WorkspaceParser(HTMLParser):
        ancestors: list[str | None]
        assistant_in_workspace = False

        def __init__(self) -> None:
            super().__init__()
            self.ancestors = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag == 'div':
                element_id = dict(attrs).get('id')
                if element_id == 'assistant-page':
                    self.assistant_in_workspace = 'main-content' in self.ancestors
                self.ancestors.append(element_id)

        def handle_endtag(self, tag: str) -> None:
            if tag == 'div' and self.ancestors:
                self.ancestors.pop()

    parser = WorkspaceParser()
    parser.feed(_page_html(True))
    assert parser.assistant_in_workspace


@pytest.mark.parametrize('theme', ['light', 'dark'])
def test_filled_save_button_has_readable_text(theme: str) -> None:
    """Catch theme text colors reused on a filled action with insufficient contrast."""
    pair = re.search(
        r'background:var\((--color-btn-success)\);[^\n\']*?color:var\((--color-[\w-]+)\)',
        page_source(),
    )
    assert pair, "filled save action must have a testable semantic color pair"
    tokens = _theme_tokens(theme)
    assert contrast_ratio(tokens[pair.group(1)], tokens[pair.group(2)]) >= 4.5


@pytest.mark.parametrize(
    ('path', 'media_type', 'signature'),
    [
        ('/static/coordinare-mark.svg', 'image/svg+xml', b'<svg'),
        ('/static/coordinare-conductor.webp', 'image/webp', b'RIFF'),
    ],
)
def test_brand_images_are_served_with_their_correct_format_and_cache_validator(
    path: str, media_type: str, signature: bytes,
) -> None:
    """Catch unshipped assets, JS-only static routing, and stale image caching."""
    from tests.unit.test_dashboard import _make_app

    client = _make_app()
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers['content-type'].startswith(media_type)
    assert response.content.startswith(signature)
    assert response.headers['cache-control'] == 'public, no-cache'
    etag = response.headers['etag']
    unchanged = client.get(path, headers={'If-None-Match': etag})
    assert unchanged.status_code == 304
    assert unchanged.content == b''


def test_navigation_features_the_mascot_and_declares_the_mark_favicon() -> None:
    """Catch mascot duplication in the header or missing identity in the drawer."""
    from html.parser import HTMLParser

    from tests.unit.test_dashboard import _make_app

    class BrandParser(HTMLParser):
        favicon: str | None = None
        mascot: str | None = None
        inside_header = False
        inside_navigation = False
        inside_toggle = False
        toggle_mascot = False
        header_image: str | None = None
        header_status = False

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            values = dict(attrs)
            if values.get('id') == 'nav-sse-dot':
                assert not self.inside_navigation, 'system status belongs in the header'
                self.header_status = self.inside_header
            if tag == 'button' and values.get('id') == 'navbar-hamburger':
                self.inside_toggle = True
            if tag == 'img' and self.inside_toggle:
                self.toggle_mascot = True
            if tag == 'header':
                self.inside_header = True
            if tag == 'nav':
                self.inside_navigation = True
            if tag == 'link' and values.get('rel') == 'icon':
                self.favicon = values.get('href')
            if tag == 'img' and self.inside_header:
                self.header_image = values.get('src')
            if tag == 'img' and self.inside_navigation:
                self.mascot = values.get('src')
                assert values.get('width') and values.get('height'), 'reserve image space before load'

        def handle_endtag(self, tag: str) -> None:
            if tag == 'button':
                self.inside_toggle = False
            if tag == 'header':
                self.inside_header = False
            if tag == 'nav':
                self.inside_navigation = False

    parser = BrandParser()
    parser.feed(_make_app().get('/').text)
    assert parser.favicon == '/static/coordinare-mark.svg'
    assert parser.mascot == '/static/coordinare-conductor.webp'
    assert parser.header_image is None
    assert parser.header_status
    assert parser.toggle_mascot, 'the mascot is the drawer button'


def test_drawer_remembers_desktop_width_without_changing_mobile_navigation(tmp_path: Path) -> None:
    """Catch a desktop drawer that cannot collapse, forgets its choice, or hides mobile links."""
    boot = re.search(r'<script data-navigation-bootstrap>(.*?)</script>', _DASHBOARD_HTML, re.DOTALL)
    assert boot is not None
    script = tmp_path / 'navigation.js'
    script.write_text(boot.group(1))
    node = shutil.which('node')
    assert node
    result = subprocess.run(
        [node, str(Path(__file__).parents[1] / 'js/dashboard_navigation_checks.js'), str(script)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
