"""176: the application script lives in /static/app.js, not inline in the page.

The page keeps exactly two inline scripts, the first-paint bootstraps. Every
other script is a ``src`` reference to a content-hashed static file, and the
application script is the last of them, where the old inline block was. This
gates that shape so application code cannot creep back into the page.
"""

from __future__ import annotations

import re

from coordinare.dashboard import _DASHBOARD_HTML, _DASHBOARD_JS_FILES, _DASHBOARD_JS_REVISIONS

_INLINE_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.DOTALL)
_BOOTSTRAPS = ["data-appearance-bootstrap", "data-navigation-bootstrap"]


def test_page_has_no_inline_script_besides_the_two_bootstraps() -> None:
    inline = [
        attrs.strip() for attrs, body in _INLINE_SCRIPT.findall(_DASHBOARD_HTML) if body.strip()
    ]
    assert sorted(inline) == sorted(_BOOTSTRAPS), (
        f"the page must keep only the first-paint bootstraps inline, found: {inline}"
    )


def test_application_script_is_the_last_static_tag_where_the_inline_block_was() -> None:
    assert _DASHBOARD_JS_FILES[-1] == "app"
    app_tag = f'<script src="/static/app.js?v={_DASHBOARD_JS_REVISIONS["app"]}"></script>'
    assert _DASHBOARD_HTML.count(app_tag) == 1
    after_app = _DASHBOARD_HTML.split(app_tag, 1)[1]
    assert after_app.lstrip().startswith("</body>"), "app.js must be the last script before </body>"
    activity = _DASHBOARD_HTML.index('<script src="/static/activity-streams.js"></script>')
    assert activity < _DASHBOARD_HTML.index(app_tag)
