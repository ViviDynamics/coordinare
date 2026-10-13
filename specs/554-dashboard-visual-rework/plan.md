# Implementation plan

Keep FastAPI and vanilla JS. No new product dependencies or backend contracts.
The approved layout changes the presentation of existing pages, not state flow.

## Files and boundaries

- templates/dashboard.html: shared shell, semantic tokens, navigation and workspace, including inline drawer bootstrap, icon rail, and
  independent mobile menu state.
- templates/dashboard.html head script: inline appearance bootstrap and lifecycle.
- dashboard/assets.py: load JS and original image bytes; preserve the shared shell
  and place the enabled assistant inside its content area.
- dashboard/routers/pages.py: serve original SVG/WebP assets with correct MIME
  types, ETag validation, and revalidation caching.
- dashboard_static/workflow.js: extracted content-hashed workflow renderer; read computed semantic colors and rerender on theme changes.
- tests/unit/test_554_dashboard_appearance.py and tests/js/dashboard_appearance_checks.js:
  execute the shipped theme logic, check theme contrast and HTML budget.

## Data and contracts

See data-model.md and contracts/appearance.md. Appearance lives only in browser
storage. All dashboard routes continue to serve the same shared shell. Existing
SSE state and control endpoints keep their schemas and behavior.

## Phases

1. Spec analysis and tests first. Tests name preference and contrast regressions.
2. Add head bootstrap, icon buttons, and theme-aware workflow rendering.
3. Reshape the shell and components with shared tokens and brand typography.
4. Browser review at 375/768/1440px in both appearances; validate routes, focus,
   board detail, menu, persisted selection, and console output.
5. Run affected tests, repository suite, lint/type checks; record any baseline
   failures explicitly. Iterate with Jason, then perform final shipping gates.

## Performance and caching

Preserve the 112KiB HTML cap. No remote fonts, appearance dependencies, or startup
queries. Retain content-hashed existing JS URLs; brand images use stable URLs with ETags
and public, no-cache revalidation. Their total byte size is bounded to 164KiB. Inline the small theme bootstrap
so it cannot arrive after first paint. Theme changes invalidate only the workflow
render cache, not the dashboard data. Existing SSE and activity limits remain.

## Constitution check

Shared tokens; WCAG AA and keyboard/mobile review; deterministic JS tests; no
new dependencies; explicit approved scope and budgets. No exceptions needed.
