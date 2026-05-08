# Research: Dashboard UX Redesign (059)

## 1. Current Dashboard Architecture

**Decision**: Work within the existing single-file architecture (`src/coordinare/dashboard.py`).  
**Rationale**: The spec and assumptions explicitly prohibit introducing a frontend build toolchain. The dashboard is a `_DASHBOARD_HTML` Python string constant served by FastAPI/Starlette. All CSS and JavaScript are inline. This constraint is pre-existing and well-understood; working within it is faster than migrating.  
**Alternatives considered**: Jinja2 templates with separate static files (adds file management complexity), Vite/React (violates spec scope).

---

## 2. SSE Snapshot — Data Available for UI Changes

All data required for the redesigned views is already present in the `build_snapshot()` return value. No new backend endpoints are needed.

### Active card / session data (per session in `active_sessions`)

| Field | Type | Notes |
|---|---|---|
| `card_id` | str | Unique session identifier |
| `card_title` | str | Human-readable card title |
| `issue_number` | int \| None | GitHub issue number |
| `issue_url` | str \| None | Link to GitHub issue |
| `pr_url` | str \| None | Link to open PR (signals awaiting-review) |
| `phase` | str | Raw phase string (e.g. `monitoring_agent`) |
| `performer_stage` | str | Raw performer sub-stage |
| `card_tokens_total` | int | Cumulative tokens for this session |
| `card_cost_estimate` | float | Running cost in USD |
| `agent_dispatch_at` | ISO str \| None | Session start time; used to compute elapsed |
| `container_id` | str \| None | Docker container ID if containerised |

**Elapsed time computation**: `Date.now() - Date.parse(agent_dispatch_at)` in JavaScript. Already used by `fmtAge()` in the existing dashboard.

**Awaiting-review detection**: A session is "awaiting review" when `pr_url` is non-null and `phase` is one of `awaiting_review`, `monitoring_pr`, or similar PR-watching phases. The existing `renderActiveWorkPanels()` already splits on this; the redesign refines the visual treatment.

**Cost display**: `card_cost_estimate` is `0.0` when unavailable (no session yet dispatched). The spec requires a distinct placeholder — use `"—"` instead of `"$0.00"` when `agent_dispatch_at` is null (no active session yet).

### Subsystem health

| Field | Notes |
|---|---|
| `subsystems[].name` | Subsystem name |
| `subsystems[].status` | `"healthy"` / `"degraded"` / `"unavailable"` |
| `subsystems[].required` | bool |
| `subsystems[].details` | Plain-language detail string or null |

**Summary logic**: Overall health = `"healthy"` if all required subsystems are healthy, `"degraded"` if any required subsystem is degraded/unavailable. Optional subsystems do not affect overall health badge.

### Idle state detection

`active_sessions` is empty AND `phase === "idle"`. The existing dashboard shows scattered empty panels in this state; the redesign replaces them with a single "Coordinare is idle" view showing last poll time, board summary, and cycle count.

---

## 3. Phase Label Rendering

**Decision**: `format_phase_label()` (Python) already converts `snake_case` → `Title Case`. The JS function `formatPhaseLabel()` mirrors this client-side.  
**Finding**: Several JS rendering paths bypass `formatPhaseLabel()` and embed raw `phase` strings directly. All call sites must be audited and updated.  
**Known bypass locations** (from code review):
- `renderActiveWorkPanels`: uses `s.phase` directly in table cell construction
- `renderSubsystems`: uses `p.name` directly (subsystem names, not phases — acceptable)
- Cycle history table: uses `entry.phase` directly in `<td>` — must be wrapped

---

## 4. CSS Design Token Approach

**Decision**: Introduce CSS custom properties (variables) in the `:root` block as the design token system.  
**Rationale**: CSS custom properties are natively supported in all modern browsers, require no build step, and can be defined once in the single `<style>` block. All scattered `#0d1117`, `#161b22`, `#58a6ff`, etc. hard-coded values will be replaced with `var(--color-*)` references.  
**Token taxonomy**:
- `--color-bg-base` (#0d1117) — page background
- `--color-bg-surface` (#161b22) — card background
- `--color-bg-elevated` (#21262d) — button / input background
- `--color-border` (#30363d) — card borders
- `--color-border-subtle` (#21262d) — table row dividers
- `--color-text-primary` (#c9d1d9) — main text
- `--color-text-muted` (#8b949e) — labels, captions
- `--color-accent-blue` (#58a6ff) — links, active nav
- `--color-accent-green` (#3fb950) — healthy, merging
- `--color-accent-yellow` (#d29922) — degraded, blocked
- `--color-accent-red` (#f85149) — error, disconnected
- `--color-phase-active` — maps to blue (monitoring phases)
- `--color-phase-success` — maps to green (merging)
- `--color-phase-warning` — maps to yellow (blocked)
- `--color-phase-error` — maps to red (recovery)
- `--elapsed-warning-threshold-ms: 1800000` — 30 minutes in ms (used in JS via getComputedStyle or a data attribute)

**Alternatives considered**: SCSS (requires build step), Tailwind (requires build step), no change (violates constitution principle III design-token requirement).

---

## 5. Elapsed-Time Threshold Flag

**Decision**: 30-minute threshold hardcoded; a card is "stale" when `Date.now() - Date.parse(agent_dispatch_at) > 30 * 60 * 1000`.  
**Visual treatment**: stale elapsed-time badges rendered with `var(--color-accent-yellow)` text and a subtle warm background — same palette as `.badge-degraded`.  
**Configurable in future**: Threshold value can be moved to a server-side config field and included in the SSE snapshot when the feature is extended.

---

## 6. Navigation Active-Page Indication

**Finding**: The existing `navigate()` JS function sets `nav-active` class on `<a>` elements using `document.querySelector('[href="'+path+'"]')`. This works but doesn't handle initial page load or direct URL navigation (e.g., opening `/history` directly in a new tab). The existing `#navbar a.nav-link.nav-active` CSS rule applies the blue underline already.  
**Decision**: Fix `navigate()` to also call the active-class setter on `DOMContentLoaded` based on `window.location.pathname`. This ensures the correct link is highlighted on hard load as well as SPA navigation.

---

## 7. Subsystem Health Summary Panel

**Decision**: Replace the always-visible subsystem table with a single-line health badge that expands to show detail on click.  
**Implementation**: `<details>/<summary>` HTML element — no JS required for expand/collapse, accessible by default, keyboard-navigable.  
**Summary line format**: `● All systems healthy` (green) or `⚠ 1 system degraded` (yellow) or `✕ 1 system unavailable` (red).  
**Detail view**: The existing table, revealed inside `<details open>` when unhealthy, or as a collapsed `<details>` when healthy.

---

## 8. Responsive Layout at 768px

**Finding**: The grid breakpoint is currently 900px. The nav hamburger fires at 767px. Between 768px–899px, the two-column grid does not engage — content is single-column, which is acceptable. The main overflow issues at 768px are:
1. `.perf-log-row` uses `grid-template-columns: 68px 82px 1fr` — fixed widths that collapse fine.
2. Inline `style="width:100%;..."` on dynamically-created tables — these are OK.
3. Symphony and config page tables with many columns — need `overflow-x: auto` wrapper.
4. The `#navbar` hamburger already handles <768px. At exactly 768px the full nav bar is shown, which is the target.

**Decision**: Add `overflow-x: auto` to table container wrappers on Symphonies and Config pages. Ensure no fixed-width element exceeds the viewport. No grid layout changes needed — the 900px breakpoint remains.

---

## 9. Performer Detail Consolidation

**Decision**: When a performer row is clicked on the home dashboard, show an inline card with: phase (human-readable), assigned card title + issue link, elapsed time (with stale flag if applicable), cost estimate, and the last 20 log lines with live update.  
**Current state**: The performer detail view (`#performers-card` detail view) shows phase, backend URL, metrics, and log — but does not show the assigned card title/link or cost estimate directly.  
**Change required**: Enrich the detail render with `card_title`, `issue_url`, and `card_cost_estimate` from the matching session in `active_sessions`. All data already present in SSE snapshot.

---

## 10. Testing Strategy

**Decision**: Unit tests for all modified/added Python helpers. JS rendering logic is not unit-testable without a browser harness (which is out of scope); acceptance is by manual visual inspection.  
**Python helpers to test**:
- `format_phase_label()` — already tested; verify extended edge cases
- `render_performer_pool_widget()` — add tests for idle, excluded, and mixed states
- `DashboardStore.build_snapshot()` — existing coverage; verify cost-placeholder logic (`card_cost_estimate=0.0` + `agent_dispatch_at=None` → UI shows `"—"`)
- Any new Python helper functions introduced (e.g., `compute_overall_health()`)

**Coverage**: Must not decrease below the project's configured threshold (90%).

---

## 11. Accessibility Notes

The constitution requires WCAG 2.1 AA. Changes to be made:
- Add `aria-label` to the SSE status dot (`<span role="status" aria-live="polite">`)
- Add `aria-current="page"` to the active nav link (supplements the visual `.nav-active` class)
- Ensure `<details>/<summary>` for subsystem health uses a visible focus ring (already present via browser default in most cases; verify)
- Elapsed-time warning must not rely on colour alone — add a `⚠` icon prefix alongside the colour change
- All interactive elements (performer rows, card rows) must have `tabindex="0"` and `role="button"` or be converted to `<button>` elements
