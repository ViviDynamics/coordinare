"""Canonical DOM reader for QA observation (spec 164 FR-011).

Labels come from HERE, never from the model. Pooling repeated model
descriptions on free-text labels is what erased the feature under test during
design; the DOM knows element text exactly.

Async on purpose: Playwright's sync API refuses to run inside a running event
loop, which is where every workflow step lives.

Lives in the performer (not the eval) so the PRODUCTION path is the tested one.
The eval imports this rather than keeping its own copy.
"""
from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)

_EXTRACT = """
() => [...document.querySelectorAll('h1,h2,h3,input,select,button,a,[role=alert]')]
  .map(el => {
    const t = el.tagName.toLowerCase();
    const kind =
      t === 'select' ? 'dropdown' :
      t === 'button' ? 'button' :
      t === 'a' ? 'link' :
      t.startsWith('h') ? 'heading' :
      t === 'input' ? (el.type === 'password' ? 'password_input' : 'text_input') :
      'banner';
    const label = (el.labels && el.labels[0] && el.labels[0].innerText)
      || el.getAttribute('name') || el.innerText || null;
    return {kind: kind, label: label ? label.trim() : null};
  })
"""


async def read_dom(url: str) -> list[dict]:
    """Return the elements rendered at *url*.

    Raises rather than returning an empty list on failure: an unreadable page
    must not be indistinguishable from an empty one, or the before/after delta
    silently compares nothing while reporting no regressions.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = await browser.new_page()
        try:
            # Round-two review, critical: the default waitUntil="load" returns
            # after HTML parsing, before client-side JavaScript has rendered
            # anything. A React/Vue app then reads as an EMPTY form at baseline
            # and a full one after the flow -- every element "removed", a false
            # regression on a working feature. networkidle waits for the app's
            # own requests to settle; the extra settle covers a final paint.
            await page.goto(url, timeout=15000, wait_until="networkidle")
            await page.wait_for_timeout(250)
            return await page.evaluate(_EXTRACT)
        except Exception as exc:
            raise RuntimeError(f"could not read the DOM at {url}: {exc}") from exc
        finally:
            await browser.close()
