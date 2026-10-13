"""Dashboard page templates and content-hashed static JS (436)."""
from __future__ import annotations

import hashlib
from pathlib import Path

DASHBOARD_HTML_BUDGET_BYTES = 112 * 1024

_DASHBOARD_JS_DIR = Path(__file__).resolve().parent / "dashboard_static"

# Original landing identity assets, shipped with the dashboard package.
_BRAND_MEDIA_TYPES = {
    "coordinare-mark.svg": "image/svg+xml",
    "coordinare-conductor.webp": "image/webp",
}
_DASHBOARD_BRAND_ASSETS = {
    name: ((_DASHBOARD_JS_DIR / name).read_bytes(), media_type)
    for name, media_type in _BRAND_MEDIA_TYPES.items()
}
_DASHBOARD_BRAND_ETAGS = {
    name: '"' + hashlib.sha256(content).hexdigest() + '"'
    for name, (content, _) in _DASHBOARD_BRAND_ASSETS.items()
}

_DASHBOARD_JS_FILES = ("helpers", "config", "performers", "workflow", "account")

def _load_dashboard_js() -> tuple[dict[str, str], dict[str, str]]:
    sources: dict[str, str] = {}
    revisions: dict[str, str] = {}
    for name in _DASHBOARD_JS_FILES:
        text = (_DASHBOARD_JS_DIR / f"{name}.js").read_text(encoding="utf-8")
        sources[name] = text
        revisions[name] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    return sources, revisions

_DASHBOARD_JS_SOURCES, _DASHBOARD_JS_REVISIONS = _load_dashboard_js()

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

_DASHBOARD_HTML_RAW = (_TEMPLATE_DIR / "dashboard.html").read_text(encoding="utf-8")
_ACTIVITY_STREAM_JS = (_TEMPLATE_DIR / "activity_streams.js").read_text(encoding="utf-8")
_ASSISTANT_FRAGMENT = (_TEMPLATE_DIR / "assistant_fragment.html").read_text(encoding="utf-8")

_DASHBOARD_HTML = _DASHBOARD_HTML_RAW.replace(
    "<!--349-static-js-->",
    "".join(
        f'<script src="/static/{name}.js?v={_DASHBOARD_JS_REVISIONS[name]}"></script>\n'
        for name in _DASHBOARD_JS_FILES
    ),
)


def _page_html(enabled: bool) -> str:
    """The dashboard page, with the assistant spliced in when it is switched on."""
    if not enabled:
        return _DASHBOARD_HTML
    marker = "</div><!-- /#main-content -->"
    return _DASHBOARD_HTML.replace(marker, _ASSISTANT_FRAGMENT + marker, 1)


def _signed_out_html(*, local_only: bool = False) -> str:
    """Reuse first-paint preference resolution without exposing authenticated pages."""
    bootstrap = _DASHBOARD_HTML_RAW.split('<script data-appearance-bootstrap>', 1)[1].split('</script>', 1)[0]
    page = (_TEMPLATE_DIR / 'signed_out.html').read_text(encoding='utf-8')
    message = 'Your dashboard session has ended.'
    if local_only:
        message += ' Your identity provider may still have you signed in.'
    return page.replace('<!--appearance-bootstrap-->', bootstrap).replace('<!--logout-message-->', message)
