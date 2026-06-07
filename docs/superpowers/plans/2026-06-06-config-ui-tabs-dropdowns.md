# Config UI: Tabs + Reference Dropdowns Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dashboard `/config` single long scroll with one tab per top-level section, and turn the five catalog-reference free-text fields into dropdowns populated from the catalogs the operator has already defined — a pure client-side affordance with no schema, API, or save-payload change.

**Architecture:** All work is inside the single `_DASHBOARD_HTML` string in `src/coordinare/dashboard.py` (vanilla client-side JS, no framework). Tabs wrap the existing per-section renders in `role="tabpanel"` divs behind a `role="tablist"` bar that reuses the dashboard's existing `.swimlane-tab` CSS; the active tab is mirrored in the URL hash (`/config#modes`) and the existing `popstate → router() → loadConfigPage()` path re-derives it on Back/reload. Reference dropdowns are a new branch in `cfgInput()` driven by a static `_CFG_REFS` label→catalog map plus an `_cfgOptions` index built from the `/api/config/all` payload already in hand. The save payload is byte-for-byte identical to today (a plain string name, or `null`/omitted for a nullable reference left at `— none —`).

**Tech Stack:** Python 3.14, pydantic 2.x, FastAPI (serves `_DASHBOARD_HTML`), vanilla ES5-style browser JS embedded as a Python string. Tests are substring assertions against `_DASHBOARD_HTML` plus the existing FastAPI `TestClient` suites — matching the established `tests/unit/test_dashboard.py` pattern (no JS execution harness exists in this repo).

---

## Background: confirmed facts the engineer needs

You will be editing exactly one Python file: `src/coordinare/dashboard.py`. Everything below lives inside the `_DASHBOARD_HTML` triple-quoted string (the JS starts around line 683; the config JS spans roughly lines 2106–2570).

**Reference DAG (all fields already exist in the schema — do NOT add or change any schema field):**

| Section id | Field label | Nullable | Options come from |
|---|---|---|---|
| `model_endpoints` | `endpoint` | no | `endpoints[].id` |
| `modes` | `tool` | no | `model_endpoints[].id` |
| `modes` | `thinking` | yes | `model_endpoints[].id` |
| `modes` | `classifier` | yes | `model_endpoints[].id` |
| `personas` | `mode` | yes | `modes[].id` |

Confirmed in `config_descriptors.py`: `setting_from_field()` sets `label=key` (line 281), so a field's `data-field` attribute equals its schema field name. Section ids are exactly `global`, `personas`, `symphonies`, `endpoints`, `model_endpoints`, `modes`, `routing`. Each section in the `/api/config/all` payload is `{id, title, kind, settings?, items?, ...}`; each catalog item is `{id, settings:[{key,label,current_value,...}], ...}`.

**Key existing JS functions (do not rename; you extend them):**
- `cfgInput(s)` — builds one editable control from a field descriptor `s`. Returns an HTML string. Reference fields are plain strings, so today they fall through to the final `<input type="text">`. (dashboard.py ~2146–2173)
- `cfgCoerce(el)` — turns a control's DOM value back into its typed JSON value. A `<select>` returns `el.value`. (~2176–2184)
- `cfgCollectChanges(container)` — collects `[data-field]` controls whose coerced value differs from `data-orig`. (~2187–2196) **Do not modify** — selects already carry `data-field`/`data-orig`/`data-type` via the shared `base` attribute string.
- `cfgFieldRow(s)` — renders a labelled row wrapping `cfgInput(s)`. (~2432)
- `cfgSectionBlock(sec)` — renders one `<section data-section=...>`; routing is delegated to `cfgRoutingSection`. (~2512)
- `loadConfigPage()` — fetches `/api/config/all`, sets `_cfgHash`/`_cfgVersion`/`_cfgRoutingAvail`, renders `header + sections.map(cfgSectionBlock)`, then `if (_cfgRoutingAvail) loadRoutingEntries();`. (~2546)
- `router()` maps `/config` → `showPage('config-page'); loadConfigPage();` (~2590). A `popstate` listener calls `router()` (~2614), so Back/forward re-runs `loadConfigPage()`.
- `esc(...)` — HTML-escapes a string (used everywhere). `CFG_INPUT` — shared control style string (~2112).
- CSS already present: `.swimlane-tabs`, `.swimlane-tab`, `.swimlane-tab.active` (dashboard.py 770–773).

**Constraints (from spec 081, preserve verbatim in behavior):** never expose/log secrets; never substitute `${VAR}` placeholders on save; atomic writes only; optimistic concurrency on every save; extend — never break — the `/api/config/*` and `/api/personas/*` endpoints; no config-schema redesign; no auth/RBAC change; no routing-binding change. Secret masking, env-placeholder literals, delete-protection badges, and 409 reload prompts must all keep working unchanged — which they do, because every section render function is reused verbatim.

**Test reality:** `tests/unit/test_dashboard.py` asserts substrings are present in `_DASHBOARD_HTML` (e.g. `assert "loadConfigPage" in _DASHBOARD_HTML`). There is no headless-browser test harness. New frontend tests therefore assert that the required functions, markup, ARIA attributes, and behavior-bearing substrings exist in the served HTML. The server-side suites (`test_dashboard_config_api.py`, `test_dashboard_config_integration.py`, `test_config_write_service.py`, `test_routing_config_service.py`, `test_config_descriptors.py`) must stay **green unmodified** — that is the non-breaking proof.

**Commands:**
- Run a single test: `.venv/bin/pytest tests/unit/test_dashboard.py::test_name -v` (use `.venv/bin/pytest`, NOT `python -m pytest`).
- Lint: `.venv/bin/ruff check tests/unit/test_dashboard.py`
- Full guard suite (run at the end): `.venv/bin/pytest tests/unit/test_dashboard.py tests/unit/test_dashboard_config_api.py tests/unit/test_dashboard_config_integration.py tests/unit/test_config_write_service.py tests/unit/test_routing_config_service.py tests/unit/test_config_descriptors.py -q`

---

## Task 0: Baseline — confirm the guard suites are green before touching anything

**Files:** none (verification only)

- [ ] **Step 1: Run the full guard suite to establish a green baseline**

Run:
```bash
.venv/bin/pytest tests/unit/test_dashboard.py tests/unit/test_dashboard_config_api.py tests/unit/test_dashboard_config_integration.py tests/unit/test_config_write_service.py tests/unit/test_routing_config_service.py tests/unit/test_config_descriptors.py -q
```
Expected: PASS (all green). If anything is red here, stop and report — it is a pre-existing failure unrelated to this work.

---

## Task 1: Reference-field dropdown rendering in `cfgInput`

Adds the static reference map, the `_cfgOptions` global, an option-hint helper, the `cfgRefSelect` builder, and a new branch in `cfgInput` that renders a `<select>` for the five reference fields.

**Files:**
- Modify: `src/coordinare/dashboard.py` (the `_DASHBOARD_HTML` config JS block: constants near ~2110, `cfgInput` ~2146)
- Test: `tests/unit/test_dashboard.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_dashboard.py`:

```python
def test_config_reference_fields_render_as_dropdowns() -> None:
    """The five catalog-reference fields are wired to render as <select> dropdowns."""
    # Static label -> catalog map drives which fields become dropdowns.
    assert "_CFG_REFS" in _DASHBOARD_HTML
    assert "cfgRefSelect" in _DASHBOARD_HTML
    assert "_cfgOptions" in _DASHBOARD_HTML
    # Every reference field label and its target catalog is declared.
    for pair in (
        "endpoint:", "tool:", "thinking:", "classifier:", "mode:",
    ):
        assert pair in _DASHBOARD_HTML, f"missing _CFG_REFS entry {pair}"
    # Nullable references offer an explicit none option.
    assert "\\u2014 none \\u2014" in _DASHBOARD_HTML or "— none —" in _DASHBOARD_HTML
    # Selects carry data-ref so coercion and option-source are discoverable in the DOM.
    assert 'data-ref="' in _DASHBOARD_HTML
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_dashboard.py::test_config_reference_fields_render_as_dropdowns -v`
Expected: FAIL (`assert "_CFG_REFS" in _DASHBOARD_HTML` — substring not present).

- [ ] **Step 3: Add the reference map + options global**

Find the constants block near the `CFG_CATALOGS` declaration (dashboard.py ~2110):

```javascript
var CFG_CATALOGS = ['endpoints', 'model_endpoints', 'modes'];
```

Immediately after that line, add:

```javascript
// --- Reference dropdowns (spec 081 UI affordance) -------------------------
// Map a reference FIELD LABEL to the catalog whose entry names are valid
// values, plus whether the field is nullable (offers a "— none —" option).
// Labels are unique across reference fields (confirmed: setting label == key),
// so a flat label-keyed map is sufficient.
var _CFG_REFS = {
  endpoint:   {catalog: 'endpoints',       nullable: false},
  tool:       {catalog: 'model_endpoints', nullable: false},
  thinking:   {catalog: 'model_endpoints', nullable: true},
  classifier: {catalog: 'model_endpoints', nullable: true},
  mode:       {catalog: 'modes',           nullable: true}
};
// Option lists for each referenceable catalog, rebuilt on every config load
// from the /api/config/all payload already in hand (no extra fetch).
var _cfgOptions = {endpoints: [], model_endpoints: [], modes: []};
```

- [ ] **Step 4: Add the option-hint helper and `cfgRefSelect`**

Insert these two functions immediately before `function cfgInput(s) {` (dashboard.py ~2146):

```javascript
// A short, self-documenting hint for one catalog option, derived only from
// fields known to exist on the schema (no invented field names): modes show
// their `tool`, model_endpoints show their `endpoint`.
function cfgOptHint(catalog, item) {
  var byLabel = {};
  (item.settings || []).forEach(function(s) { byLabel[s.label] = s.current_value; });
  if (catalog === 'modes' && byLabel.tool) return 'tool=' + byLabel.tool;
  if (catalog === 'model_endpoints' && byLabel.endpoint) return 'endpoint=' + byLabel.endpoint;
  return '';
}

// Build a <select> for a reference field. Honors nullability with a "— none —"
// option, and PRESERVES + FLAGS a stored value that is not in the catalog so a
// save of unrelated fields never silently discards it.
function cfgRefSelect(s, refSpec, base) {
  var cur = (s.current_value === null || s.current_value === undefined) ? '' : String(s.current_value);
  var opts = _cfgOptions[refSpec.catalog] || [];
  var html = '';
  if (refSpec.nullable) {
    html += '<option value=""' + (cur === '' ? ' selected' : '') + '>— none —</option>';
  }
  var known = false;
  opts.forEach(function(o) {
    if (cur === o.value) known = true;
    var label = o.hint ? (o.value + ' — ' + o.hint) : o.value;
    html += '<option value="' + esc(o.value) + '"' + (cur === o.value ? ' selected' : '') + '>'
      + esc(label) + '</option>';
  });
  if (cur !== '' && !known) {
    html = '<option value="' + esc(cur) + '" selected>' + esc(cur) + '  ⚠ not in catalog</option>' + html;
  }
  return '<select ' + base + ' data-ref="' + esc(refSpec.catalog) + '" style="' + CFG_INPUT + '">'
    + html + '</select>';
}
```

- [ ] **Step 5: Branch `cfgInput` to use `cfgRefSelect` for reference fields**

In `cfgInput(s)`, the `base` attribute string is built first. Immediately after the `base` assignment and BEFORE the `if (s.type === 'bool')` check, add the reference branch. The relevant existing code:

```javascript
function cfgInput(s) {
  var orig = (s.current_value === undefined) ? null : s.current_value;
  var base = 'data-field="' + esc(s.label) + '" data-type="' + esc(s.type) + '"'
    + ' data-orig="' + esc(JSON.stringify(orig)) + '"';
  if (s.type === 'bool') {
```

becomes:

```javascript
function cfgInput(s) {
  var orig = (s.current_value === undefined) ? null : s.current_value;
  var base = 'data-field="' + esc(s.label) + '" data-type="' + esc(s.type) + '"'
    + ' data-orig="' + esc(JSON.stringify(orig)) + '"';
  var refSpec = _CFG_REFS[s.label] || _CFG_REFS[s.key];
  if (refSpec) {
    return cfgRefSelect(s, refSpec, base);
  }
  if (s.type === 'bool') {
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_dashboard.py::test_config_reference_fields_render_as_dropdowns -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/coordinare/dashboard.py tests/unit/test_dashboard.py
git commit -m "feat(config-ui): render catalog-reference fields as dropdowns"
```

---

## Task 2: Populate `_cfgOptions` from the config payload on load

`cfgRefSelect` reads `_cfgOptions`; this task fills it from the sections already fetched by `loadConfigPage`, so the dropdowns show the operator's real catalog entries.

**Files:**
- Modify: `src/coordinare/dashboard.py` (`loadConfigPage` ~2546; new helper `cfgBuildOptions`)
- Test: `tests/unit/test_dashboard.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_dashboard.py`:

```python
def test_config_options_index_built_from_payload() -> None:
    """loadConfigPage rebuilds the dropdown option index from the fetched sections."""
    assert "cfgBuildOptions" in _DASHBOARD_HTML
    # The builder is invoked during load (before sections are rendered).
    assert "_cfgOptions = cfgBuildOptions(" in _DASHBOARD_HTML
    # Options carry both the stored value and a display hint.
    assert "cfgOptHint(" in _DASHBOARD_HTML
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_dashboard.py::test_config_options_index_built_from_payload -v`
Expected: FAIL (`cfgBuildOptions` not present).

- [ ] **Step 3: Add the `cfgBuildOptions` helper**

Insert immediately before `function cfgOptHint(catalog, item) {` (added in Task 1):

```javascript
// Build the {endpoints, model_endpoints, modes} option index from the loaded
// sections. Each option is {value, hint}; only referenceable catalogs are kept.
function cfgBuildOptions(sections) {
  var idx = {endpoints: [], model_endpoints: [], modes: []};
  (sections || []).forEach(function(sec) {
    if (idx[sec.id] === undefined) return;
    idx[sec.id] = (sec.items || []).map(function(it) {
      return {value: it.id, hint: cfgOptHint(sec.id, it)};
    });
  });
  return idx;
}
```

- [ ] **Step 4: Build the index inside `loadConfigPage` before rendering**

In `loadConfigPage`, the existing lines are:

```javascript
  _cfgHash = (data.content_hashes || {}).config_yaml || null;
  _cfgVersion = data.config_version;
  _cfgRoutingAvail = !!data.routing_available;
```

Add the options-build line immediately after them:

```javascript
  _cfgHash = (data.content_hashes || {}).config_yaml || null;
  _cfgVersion = data.config_version;
  _cfgRoutingAvail = !!data.routing_available;
  _cfgOptions = cfgBuildOptions(data.sections);
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_dashboard.py::test_config_options_index_built_from_payload -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/coordinare/dashboard.py tests/unit/test_dashboard.py
git commit -m "feat(config-ui): build dropdown option index from config payload"
```

---

## Task 3: Coerce a nullable `— none —` selection to null on save

A nullable reference left at `— none —` has `value === ''`. `cfgCoerce` currently returns `''` for a string control, which would serialize an empty string rather than omitting/nulling the field. This task makes an empty reference select coerce to `null`, matching the design's "serializes to omitting the field / null."

**Files:**
- Modify: `src/coordinare/dashboard.py` (`cfgCoerce` ~2176)
- Test: `tests/unit/test_dashboard.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_dashboard.py`:

```python
def test_config_empty_reference_coerces_to_null() -> None:
    """An empty reference <select> (— none —) coerces to null, not an empty string."""
    # cfgCoerce special-cases controls carrying data-ref with an empty value.
    assert "getAttribute('data-ref')" in _DASHBOARD_HTML
    # The null branch appears in the coercion path.
    assert "return null" in _DASHBOARD_HTML
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_dashboard.py::test_config_empty_reference_coerces_to_null -v`
Expected: FAIL (`getAttribute('data-ref')` not present in `cfgCoerce`).

- [ ] **Step 3: Add the null-coercion branch**

The existing `cfgCoerce`:

```javascript
function cfgCoerce(el) {
  var t = el.getAttribute('data-type');
  if (t === 'bool') return el.checked;
  var v = el.value;
  if (t === 'int') return v.trim() === '' ? null : parseInt(v, 10);
  if (t === 'float') return v.trim() === '' ? null : parseFloat(v);
  if (t === 'list') return v.split(',').map(function(x) { return x.trim(); }).filter(function(x) { return x.length; });
  return v;
}
```

becomes (insert the reference check right after reading `v`):

```javascript
function cfgCoerce(el) {
  var t = el.getAttribute('data-type');
  if (t === 'bool') return el.checked;
  var v = el.value;
  // A nullable reference left at "— none —" serializes as null (field omitted),
  // matching the prior free-text-empty-means-unset behavior.
  if (el.getAttribute('data-ref') && v === '') return null;
  if (t === 'int') return v.trim() === '' ? null : parseInt(v, 10);
  if (t === 'float') return v.trim() === '' ? null : parseFloat(v);
  if (t === 'list') return v.split(',').map(function(x) { return x.trim(); }).filter(function(x) { return x.length; });
  return v;
}
```

Note: `data-orig` for an unset nullable reference is `JSON.stringify(null)` → `"null"`, and `cfgCoerce` now returns `null` → `JSON.stringify(null)` → `"null"`, so an untouched `— none —` field correctly registers as no change in `cfgCollectChanges`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_dashboard.py::test_config_empty_reference_coerces_to_null -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/coordinare/dashboard.py tests/unit/test_dashboard.py
git commit -m "feat(config-ui): coerce empty reference select to null on save"
```

---

## Task 4: Tab bar + tab-panel rendering in `loadConfigPage`

Wrap each section render in a `role="tabpanel"` div, prepend a `role="tablist"` bar reusing `.swimlane-tab` CSS, and show only the active panel. Active tab is derived from the URL hash (default: first section, `global`).

**Files:**
- Modify: `src/coordinare/dashboard.py` (new `_cfgTab`, `cfgTabId`, `cfgPanelId`, `cfgTabBar`, `cfgSelectTab`; `loadConfigPage` render line ~2568)
- Test: `tests/unit/test_dashboard.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_dashboard.py`:

```python
def test_config_page_renders_tabbed_layout() -> None:
    """The config page renders a tablist + one tabpanel per section, not a long scroll."""
    assert "cfgTabBar" in _DASHBOARD_HTML
    assert "cfgSelectTab" in _DASHBOARD_HTML
    assert 'role="tablist"' in _DASHBOARD_HTML
    assert 'role="tab"' in _DASHBOARD_HTML
    assert 'role="tabpanel"' in _DASHBOARD_HTML
    # Tabs reuse the existing swimlane tab styling — no new visual language.
    assert "swimlane-tabs" in _DASHBOARD_HTML
    # Each section is wrapped in a panel and the active tab is hash-derived.
    assert "cfgPanelId(" in _DASHBOARD_HTML
    assert "location.hash" in _DASHBOARD_HTML
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_dashboard.py::test_config_page_renders_tabbed_layout -v`
Expected: FAIL (`cfgTabBar` not present).

- [ ] **Step 3: Add the tab-state global and id helpers**

Add to the constants block, immediately after the `_cfgOptions` declaration added in Task 1:

```javascript
var _cfgTab = null;   // active config section id (tab)
function cfgTabId(secId)   { return 'cfg-tab-' + secId; }
function cfgPanelId(secId) { return 'cfg-panel-' + secId; }
```

- [ ] **Step 4: Add `cfgTabBar` and `cfgSelectTab`**

Insert immediately before `async function loadConfigPage() {` (dashboard.py ~2546):

```javascript
// Render the horizontal tab strip. Reuses .swimlane-tab styling. WAI-ARIA tabs
// pattern: roving tabindex, aria-selected/aria-controls on each tab.
function cfgTabBar(sections, active) {
  var tabs = (sections || []).map(function(sec) {
    var on = sec.id === active;
    return '<button type="button" role="tab" id="' + cfgTabId(sec.id) + '"'
      + ' aria-selected="' + (on ? 'true' : 'false') + '"'
      + ' aria-controls="' + cfgPanelId(sec.id) + '"'
      + ' tabindex="' + (on ? '0' : '-1') + '"'
      + ' class="swimlane-tab' + (on ? ' active' : '') + '"'
      + ' onclick="cfgSelectTab(&quot;' + esc(sec.id) + '&quot;)"'
      + ' onkeydown="cfgTabKey(event, &quot;' + esc(sec.id) + '&quot;)">'
      + esc(sec.title) + '</button>';
  }).join('');
  return '<div class="swimlane-tabs" role="tablist" aria-label="Configuration sections">'
    + tabs + '</div>';
}

// Switch the visible panel. pushState so the browser Back button moves between
// tabs (the existing popstate -> router() -> loadConfigPage() re-derives state).
function cfgSelectTab(secId) {
  _cfgTab = secId;
  document.querySelectorAll('#config-page-section [role="tab"]').forEach(function(t) {
    var on = t.id === cfgTabId(secId);
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', on ? 'true' : 'false');
    t.setAttribute('tabindex', on ? '0' : '-1');
  });
  document.querySelectorAll('#config-page-section [role="tabpanel"]').forEach(function(p) {
    p.hidden = (p.id !== cfgPanelId(secId));
  });
  if (location.hash !== '#' + secId) {
    history.pushState(null, '', '/config#' + secId);
  }
}
```

- [ ] **Step 5: Render the tab bar + panels in `loadConfigPage`**

The existing render tail of `loadConfigPage`:

```javascript
  var header = '<div style="font-size:11px;color:var(--color-text-muted);margin-bottom:12px">'
    + 'Config version ' + esc(String(data.config_version))
    + (data.routing_available ? ' · routing available' : ' · routing unavailable')
    + '</div>';
  el.innerHTML = header + (data.sections || []).map(cfgSectionBlock).join('');
  if (_cfgRoutingAvail) loadRoutingEntries();
```

becomes:

```javascript
  var header = '<div style="font-size:11px;color:var(--color-text-muted);margin-bottom:12px">'
    + 'Config version ' + esc(String(data.config_version))
    + (data.routing_available ? ' · routing available' : ' · routing unavailable')
    + '</div>';
  var sections = data.sections || [];
  var hash = (location.hash || '').replace(/^#/, '');
  var active = sections.some(function(s) { return s.id === hash; })
    ? hash
    : (sections[0] ? sections[0].id : null);
  _cfgTab = active;
  var panels = sections.map(function(sec) {
    return '<div role="tabpanel" id="' + cfgPanelId(sec.id) + '"'
      + ' aria-labelledby="' + cfgTabId(sec.id) + '"'
      + (sec.id === active ? '' : ' hidden') + '>'
      + cfgSectionBlock(sec) + '</div>';
  }).join('');
  el.innerHTML = header + cfgTabBar(sections, active) + panels;
  if (_cfgRoutingAvail) loadRoutingEntries();
```

Note: `loadRoutingEntries()` queries elements by id and works whether or not the routing panel is hidden, so deep-linking to a non-routing tab still wires routing correctly when the operator switches to it.

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_dashboard.py::test_config_page_renders_tabbed_layout -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/coordinare/dashboard.py tests/unit/test_dashboard.py
git commit -m "feat(config-ui): tabbed layout for config sections"
```

---

## Task 5: Keyboard navigation between tabs (`cfgTabKey`)

`cfgTabBar` references `cfgTabKey` in its `onkeydown`; this task implements arrow/Home/End navigation per the WAI-ARIA tabs pattern.

**Files:**
- Modify: `src/coordinare/dashboard.py` (new `cfgTabKey`, placed next to `cfgSelectTab`)
- Test: `tests/unit/test_dashboard.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/test_dashboard.py`:

```python
def test_config_tabs_support_keyboard_navigation() -> None:
    """Tab strip supports Arrow/Home/End keyboard navigation (WAI-ARIA tabs)."""
    assert "function cfgTabKey(" in _DASHBOARD_HTML
    assert "ArrowRight" in _DASHBOARD_HTML
    assert "ArrowLeft" in _DASHBOARD_HTML
    assert "Home" in _DASHBOARD_HTML
    assert "End" in _DASHBOARD_HTML
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_dashboard.py::test_config_tabs_support_keyboard_navigation -v`
Expected: FAIL (`function cfgTabKey(` not present).

- [ ] **Step 3: Implement `cfgTabKey`**

Insert immediately after `cfgSelectTab` (added in Task 4):

```javascript
// Arrow/Home/End keyboard navigation across the tab strip (WAI-ARIA tabs).
function cfgTabKey(event, secId) {
  var key = event.key;
  if (key !== 'ArrowRight' && key !== 'ArrowLeft' && key !== 'Home' && key !== 'End') return;
  event.preventDefault();
  var tabs = Array.prototype.slice.call(
    document.querySelectorAll('#config-page-section [role="tab"]'));
  if (!tabs.length) return;
  var i = tabs.findIndex(function(t) { return t.id === cfgTabId(secId); });
  var next;
  if (key === 'Home') next = 0;
  else if (key === 'End') next = tabs.length - 1;
  else if (key === 'ArrowRight') next = (i + 1) % tabs.length;
  else next = (i - 1 + tabs.length) % tabs.length;
  var target = tabs[next];
  if (!target) return;
  var targetSec = target.id.replace('cfg-tab-', '');
  cfgSelectTab(targetSec);
  target.focus();
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_dashboard.py::test_config_tabs_support_keyboard_navigation -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/coordinare/dashboard.py tests/unit/test_dashboard.py
git commit -m "feat(config-ui): keyboard navigation for config tabs"
```

---

## Task 6: Stale-reference preserve-and-flag regression test

`cfgRefSelect` (Task 1) already injects an unknown stored value as a flagged, selected option. This task locks that behavior in with an explicit test so a future refactor cannot silently drop it.

**Files:**
- Test only: `tests/unit/test_dashboard.py`

- [ ] **Step 1: Write the test**

Add to `tests/unit/test_dashboard.py`:

```python
def test_config_stale_reference_is_preserved_and_flagged() -> None:
    """A stored reference not in the catalog is kept as a selected, flagged option."""
    # cfgRefSelect injects the unknown current value as a selected option ...
    assert "not in catalog" in _DASHBOARD_HTML
    # ... and only when it is non-empty and unmatched (preserve, never drop).
    assert "if (cur !== '' && !known)" in _DASHBOARD_HTML
```

- [ ] **Step 2: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_dashboard.py::test_config_stale_reference_is_preserved_and_flagged -v`
Expected: PASS (the implementation already exists from Task 1).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_dashboard.py
git commit -m "test(config-ui): lock in stale-reference preserve-and-flag behavior"
```

---

## Task 7: Final verification — guard suites green, size budget, lint, a11y checklist

**Files:**
- Possibly modify: `src/coordinare/dashboard.py` (only if the size-budget test fails)
- Possibly modify: `specs/081-config-ui/checklists/ux-accessibility.md` (re-tick items)

- [ ] **Step 1: Run the size-budget test**

Run: `.venv/bin/pytest tests/unit/test_dashboard.py::test_dashboard_html_under_140kb -v`
Expected: PASS. If it FAILS (the added JS pushed `_DASHBOARD_HTML` over 140 KB), do NOT silently raise the limit — report the new size and ask before changing the budget.

- [ ] **Step 2: Run the full guard suite**

Run:
```bash
.venv/bin/pytest tests/unit/test_dashboard.py tests/unit/test_dashboard_config_api.py tests/unit/test_dashboard_config_integration.py tests/unit/test_config_write_service.py tests/unit/test_routing_config_service.py tests/unit/test_config_descriptors.py -q
```
Expected: PASS (all green). The server-side suites must be green **unmodified** — that proves the redesign is non-breaking. If any server-side test required a change, stop: it means the contract moved, which is out of scope.

- [ ] **Step 3: Lint the touched test file**

Run: `.venv/bin/ruff check tests/unit/test_dashboard.py`
Expected: no errors.

- [ ] **Step 4: Re-run the spec-081 UX accessibility checklist**

Open `specs/081-config-ui/checklists/ux-accessibility.md` and re-verify each item against the tabbed layout:
- Tab strip is `role="tablist"`; tabs are `role="tab"` with `aria-selected` + `aria-controls`; panels are `role="tabpanel"` with `aria-labelledby` (Tasks 4–5).
- Roving tabindex (only the active tab is `tabindex="0"`) and Arrow/Home/End navigation work (Task 5).
- `#config-page-section` retains `aria-live="polite"` (unchanged — verify still present in the static HTML around the `config-page-section` container).
- Dropdowns are real `<select>` elements (keyboard- and screen-reader-native); the `— none —` and `⚠ not in catalog` options are plain text, no color-only signaling.

Tick the items that pass. If any item regressed, fix it inline (add a task-style fix) before sign-off.

- [ ] **Step 5: Commit any checklist updates**

```bash
git add specs/081-config-ui/checklists/ux-accessibility.md
git commit -m "docs(config-ui): re-verify UX accessibility checklist for tabbed layout"
```

---

## Self-review (completed by plan author)

**Spec coverage** (against `docs/superpowers/specs/2026-06-06-config-ui-tabs-dropdowns-design.md`):
- §1 Tab navigation → Tasks 4 (panels + hash-derived active + reuse `.swimlane-tab`) & 5 (ARIA roles, roving tabindex, keyboard nav). Hash sync + Back via pushState/popstate covered in Task 4 Step 4–5.
- §2 Reference dropdowns: five fields → Task 1 (`_CFG_REFS` table matches the design's 5-row table exactly). Nullable `— none —` → Task 1 + Task 3 (coerce to null). Stale preserve+flag → Task 1 + Task 6. Self-documenting labels → Task 1 `cfgOptHint`. Save payload unchanged → Task 3 note + `cfgCollectChanges` left untouched.
- §3 Data flow unchanged → no API/endpoint edits anywhere; options built from the existing payload (Task 2).
- §4 Error handling: inline errors unchanged (render functions reused verbatim); empty-catalog dropdown shows `— none —` only (nullable) or no options (non-nullable) — emergent from `cfgRefSelect`.
- §5 Testing: frontend substring tests added (Tasks 1–6); server suites green unmodified + a11y checklist re-run (Task 7).
- Scope guard: no schema/API/auth/routing-binding change — honored (only `cfgInput`/`cfgCoerce`/`loadConfigPage` extended, plus pure-additive helpers).

**Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to" — every code step shows complete code. ✓

**Type/name consistency:** `_CFG_REFS`, `_cfgOptions`, `_cfgTab`, `cfgBuildOptions`, `cfgOptHint`, `cfgRefSelect`, `cfgTabId`, `cfgPanelId`, `cfgTabBar`, `cfgSelectTab`, `cfgTabKey` — used identically across tasks. `cfgRefSelect` is defined in Task 1 and referenced by Task 6's test; `cfgTabKey` referenced in Task 4's `cfgTabBar` and defined in Task 5 (forward reference is fine — both land before any user interaction). Option object shape `{value, hint}` consistent between `cfgBuildOptions` (Task 2) and `cfgRefSelect` (Task 1). ✓
