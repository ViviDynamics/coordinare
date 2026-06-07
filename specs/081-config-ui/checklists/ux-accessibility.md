# UX Accessibility Checklist: Live Config Editing in the Dashboard UI

**Purpose**: Constitution v1.1.0 Principle III sign-off gate for the Config view — verify
WCAG 2.1 AA conformance, design-token sourcing, and inline-error / loading-state coverage
before the feature is considered done (T043).
**Created**: 2026-06-06
**Feature**: [spec.md](../spec.md) | **Quickstart**: [quickstart.md](../quickstart.md)
**Surface under review**: the Config view in `src/coordinare/dashboard.py` (`_DASHBOARD_HTML`)
and its `/api/config/*` + `/api/personas/*` backing endpoints.

## Design-Token Sourcing (no hard-coded visual values)

- [x] All Config-view colors/spacing/typography come from the existing `var(--…)` design
      tokens, not literal hex/px values (358 `var(--` usages in `dashboard.py`; the Config
      view reuses the same token palette as the rest of the dashboard)
- [x] Editable vs. read-only/derived settings are distinguished with token-driven styling
      (no raw color literals introduced for the read-only state)
- [x] Masked-secret fields reuse the shared input/token styling (no bespoke colors)
- [x] No new inline `style="color:#…"` / `px` literals were added for Config-view states
      (statuses reuse `--color-text-muted` and sibling status tokens)

## Perceivable (WCAG 2.1 AA)

- [x] Text/UI contrast inherits the dashboard's AA-compliant token palette (no lower-contrast
      overrides introduced for the Config view)
- [x] Information is not conveyed by color alone — editable/read-only and
      hot-reloaded/staged-restart states carry a text label in addition to any color cue
- [x] Form controls have programmatic labels (`<label>` / `aria-label`); secret fields are
      labeled as masked so a screen reader announces the masking, not the value
- [x] The masked secret value (`••••••`) and `${VAR}` literals are rendered as text, never as
      a placeholder that hides them from assistive tech

## Operable (WCAG 2.1 AA)

- [x] All Config-view actions (edit, save, catalog create/edit/delete, routing CRUD) are
      keyboard reachable and operable — they are native `<button>`/`<input>`/`<select>`
      controls, not click-only `<div>`s
- [x] Focus order follows the visual section order (global → personas → symphonies →
      endpoints → model_endpoints → modes → routing); the sections are now presented as a
      tab strip, and only the active tab's panel is in the tab order (inactive panels are
      `hidden`)
- [x] The section tabs implement the WAI-ARIA tabs pattern: tab strip is `role="tablist"`,
      each tab is `role="tab"` with `aria-selected` + `aria-controls`, each panel is
      `role="tabpanel"` with `aria-labelledby`; roving tabindex keeps only the active tab at
      `tabindex="0"` and Arrow/Home/End move focus across tabs (`cfgTabBar`/`cfgSelectTab`/
      `cfgTabKey` in `dashboard.py`)
- [x] Reference fields (model_endpoint `endpoint`; mode `tool`/`thinking`/`classifier`;
      persona `mode`) are native `<select>` dropdowns; `— none —` and `⚠ not in catalog`
      are rendered as plain option text, keyboard-operable like any select
- [x] Disabled actions (e.g. delete-while-referenced, save with no changes) use the native
      `disabled` attribute so they are skipped by keyboard/AT rather than silently inert
- [x] No keyboard trap is introduced by the inline-edit / save-state controls or the tab strip

## Understandable (WCAG 2.1 AA)

- [x] Every setting shows a human label + help text (`Field(description=…)` → descriptor
      `label`/`help`), so the purpose of each control is clear (quickstart §1)
- [x] Inline validation errors are specific and actionable (e.g. "Must be between 1 and 3600
      seconds."), secret-free, and stack-trace-free (FR-/error-model, `errors[].message`)
- [x] The hot-reloaded vs. staged-restart outcome is stated in words after a save, and the
      routing "applies to the next performer job" note is shown explicitly (quickstart §5)
- [x] The optimistic-concurrency 409 surfaces a plain-language "changed on disk since you
      loaded it — reload and re-apply" message, not an HTTP code alone (quickstart §3)

## Robust — Status, Errors & Loading States (announced to AT)

- [x] The Config page region uses `aria-live="polite"` so async section loads and state
      changes are announced (`#config-page-section`, `dashboard.py:1064`)
- [x] A `role="status" aria-live="polite"` region exists for transient status (header status
      region, `dashboard.py:906`); save success/failure feedback is rendered into a live region
- [x] A visible **loading state** is shown while sections fetch (`<span class="empty-state">
      Loading...</span>`); the view degrades to that state rather than blank-on-error
- [x] The routing **read-only empty state** (when `routing_available` is false) is explanatory
      guidance, not an error, and is announced as content rather than an alert (T019)
- [x] Inline field errors are associated with their control (rendered adjacent / referenced)
      so the error is discoverable from the focused input, not only visually

## Notes

- The Config view is additive HTML/JS inside the existing `_DASHBOARD_HTML` shell; it inherits
  the dashboard's established token system, focus styling, and two `aria-live` regions rather
  than introducing a parallel visual language. This checklist verifies the new surface does
  not regress those guarantees and adds the Config-specific label/help, inline-error, and
  loading-state coverage Constitution III requires.
- Re-verified against the tabbed layout (UI-only tabs + reference dropdowns, on the
  `081-config-ui` branch): the single long scroll is replaced by a `role="tablist"` tab strip
  reusing the existing `.swimlane-tab` styling, with roving tabindex and Arrow/Home/End
  keyboard navigation, and the five free-text reference fields are now native `<select>`
  dropdowns. These changes strengthen the Operable/Robust guarantees (one panel visible at a
  time, fully keyboard-operable selects) without altering the save payload, token sourcing, or
  the `aria-live` regions, so no item regressed.
- Backing endpoints enforce the substance behind the UI affordances: secrets are masked and
  `${VAR}` literals preserved server-side (so AT never receives a real secret), and the error
  model is secret-free and stack-trace-free (verified by
  `tests/unit/test_config_write_service.py` and `tests/unit/test_dashboard_config_api.py`).
- Full manual screen-reader certification (NVDA/VoiceOver) against a live dashboard is recorded
  as part of the quickstart end-to-end validation (T045); the items above are verifiable from
  the markup/endpoint contract and the automated tests.
