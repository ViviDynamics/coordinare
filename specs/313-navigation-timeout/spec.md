# Issue 313: Shared browser navigation timeout

Loaded CI runners can take more than three seconds to load dashboard pages.
Navigation must allow that delay without extending assertion waits.

The shared Playwright context fixture in `tests/e2e/conftest.py` sets a
15,000 millisecond default navigation timeout. Pages inherit it for `goto`,
`reload`, and other navigations. Explicit navigation timeouts are removed.
Existing assertion timeouts, including `_WAIT_SSE`, stay unchanged.

Acceptance:

1. Every fixture page inherits the shared 15 second navigation timeout.
2. No navigation call supplies a shorter timeout or uses `_WAIT_SSE`.
3. Assertion waits retain their existing values.
4. A unit guard scans every Python file under `tests/e2e` using the AST and
   rejects explicit `goto` or `reload` timeouts, including named constants and
   multiline calls. Requiring the shared default also prevents shorter overrides.
5. Unit tests verify context configuration without launching a browser.

Browser execution is reserved for the maintainer. This change affects tests only.
