# Config UI redesign: tabs + reference dropdowns

**Date:** 2026-06-06
**Branch:** `081-config-ui` (extends spec 081; no new spec)
**Status:** Approved — proceeding to implementation plan

## Problem

The dashboard `/config` view renders all seven top-level config sections
(Global, Personas, Symphonies, Endpoints, Model Endpoints, Modes, Routing) into
one long vertically scrolling `#config-page-section`. Two pain points:

1. **No navigation** — finding a section means scrolling past every other one.
2. **References are retyped** — fields that point at a catalog entry by name
   (e.g. a persona's `mode`, a mode's `tool`) are free-text inputs. The operator
   must remember and retype exact names that already exist elsewhere in the
   config, with no protection against typos.

## Goals

- Each top-level section gets its own tab; the long scroll is gone.
- Catalog-reference fields become dropdowns populated from the catalogs the
  operator has already defined — "select, don't retype."
- **Strictly UI-only.** No config-schema change, no API change, no auth change,
  no routing-binding change. This honors spec 081's "no schema redesign"
  constraint and lands on the existing `081-config-ui` branch.

## Non-goals

- Adding any new persistable field (e.g. a *new* persona→model binding). The
  persona `mode` field already exists in the schema (added in spec 080); we only
  change how it is **edited**, not what is stored.
- Changing server-side validation, referential integrity, secret handling,
  optimistic concurrency, or the routing "binds at next job" semantics.

## Design

### 1. Tab navigation

Wrap the seven existing section renders in a horizontal tab bar reusing the
dashboard's existing `.swimlane-tabs` / `.swimlane-tab` / `.swimlane-tab.active`
CSS (already defined in `dashboard.py`). No new visual language.

- Tabs: **Global · Personas · Symphonies · Endpoints · Model Endpoints · Modes ·
  Routing**.
- Switching tabs toggles an `active` class to show one panel and hide the
  others. Every existing section render function is reused unchanged — secret
  masking, `${VAR}` raw literals, "referenced by:" delete-protection badges, and
  optimistic-concurrency saves all carry over verbatim.
- The active tab is reflected in the URL hash (`/config#modes`) so reload and
  deep-links reopen the same tab and the browser Back button moves between tabs.
  Default tab (no hash, or unknown hash) is **Global**.
- Accessibility: the tab strip uses `role="tablist"`, each tab `role="tab"` with
  `aria-selected` and `aria-controls`; each panel `role="tabpanel"` with
  `aria-labelledby`. Arrow-key navigation between tabs per the WAI-ARIA tabs
  pattern. The `#config-page-section` keeps its `aria-live="polite"`.

### 2. Reference dropdowns (UI affordance only)

Five existing free-text reference fields become `<select>` elements populated
client-side from the catalog data already present in the `/api/config/all`
payload — no new API calls.

| Tab | Field | Options sourced from |
|---|---|---|
| Model Endpoints | `endpoint` | `endpoints[].name` |
| Modes | `tool` | `model_endpoints[].name` |
| Modes | `thinking` (nullable) | `model_endpoints[].name` |
| Modes | `classifier` (nullable) | `model_endpoints[].name` |
| Personas | `mode` (nullable) | `modes[].name` |

Behavior:

- **Nullable fields** (`thinking`, `classifier`, persona `mode`) include a
  `— none —` option that serializes to omitting the field / `null`, matching
  current free-text-empty behavior.
- **Stale / unknown references — preserve + flag.** If the stored value is not in
  the catalog (e.g. the referenced mode was deleted), it is injected as the
  selected option, flagged `⚠ not in catalog`. It stays selected so saving other
  fields never silently discards it. The operator may re-point it at a valid
  entry or leave it.
- **Self-documenting options.** Each option label hints at what it resolves to,
  e.g. `single-qwen — single · tool=qwen-coder`, so the operator sees the effect
  without opening another tab.
- **Save payload unchanged.** The select still submits the plain string name.
  Server-side validation, referential integrity, secret handling, and
  concurrency guards are untouched — they cannot tell the value came from a
  dropdown rather than a text box.

### 3. Data flow

Unchanged on the wire. `GET /api/config/all` already returns every catalog, so
the client has all the option lists it needs at render time. Saves still go
through the existing `PUT /api/config/section/{id}`,
`POST/PUT/DELETE /api/config/catalog/{catalog}[/{item_id}]`, and
`PUT/DELETE /api/personas/{role}` endpoints with identical payloads.

### 4. Error handling

- Server validation errors (422), conflicts (409), and unknown-reference /
  delete-protection responses render inline exactly as today.
- A reference whose catalog is empty (e.g. a persona `mode` dropdown when no
  modes are defined) shows only `— none —` plus any preserved stale value, with
  helper text pointing to the Modes tab.

### 5. Testing

- **New tests are frontend-behavior assertions only**, added to the existing
  dashboard test file following the established MagicMock-daemon harness pattern:
  tab switching (hash sync, one panel visible), dropdown population from
  catalogs, `— none —` round-trip, and stale-ref preserve-and-flag.
- The existing API/integration suites
  (`test_dashboard_config_api.py`, `test_dashboard_config_integration.py`,
  `test_config_write_service.py`, `test_routing_config_service.py`,
  `test_config_descriptors.py`) must stay **green unmodified** — that is the
  proof the redesign is non-breaking.
- Re-run the spec-081 UX accessibility checklist
  (`specs/081-config-ui/checklists/ux-accessibility.md`) against the tabbed
  layout (Constitution Principle III sign-off).

## Risks / trade-offs

- **Hash-based tab state** could collide with any future in-page anchor use;
  acceptable since the config view has none today.
- **Preserve+flag stale refs** means an invalid value can persist through a save
  of unrelated fields. This is intentional (no silent data loss) and the flag
  makes it visible; server validation still blocks saving an invalid value into
  the field that owns the reference.

## Scope guard

No schema redesign · no API changes · no auth/RBAC changes · no routing-binding
changes · all work on the `081-config-ui` branch.
