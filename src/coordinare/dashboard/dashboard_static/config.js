// 349: the Settings page's editors (the General, Agents, catalog and routing
// sections), served verbatim from /static/config.js. Only the config pages call
// into it, so the main dashboard never pays for these bytes.

// 143: what a diff shows for one value. A list reads as its items; an unset value
// as "(unset)". Secrets never reach this function: cfgDiffRows hides them first.
function cfgDiffText(value) {
  if (value === null || value === undefined) return '(unset)';
  if (Array.isArray(value)) return value.length ? value.join(', ') : '(empty)';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

// 143: one row per setting whose value differs, old to new. A secret is reported as
// changed, never with either value. Pure, so the node check runs it as it ships.
function cfgDiffRows(items) {
  return (items || []).filter(function(it) {
    return JSON.stringify(it.before) !== JSON.stringify(it.after);
  }).map(function(it) {
    if (it.secret) return {label: it.label, before: '(hidden)', after: '(hidden)', secret: true};
    return {label: it.label, before: cfgDiffText(it.before), after: cfgDiffText(it.after), secret: false};
  });
}

// 143: the reader's words for a save the server refused. A 409 with conflict set
// means someone else changed the config since this page loaded; a 428 means the
// page sent no version at all. Any other 409 is a clash such as a duplicate name,
// and keeps the server's text. Pure, so the node check runs it as it ships.
function cfgConflictMessage(status, body) {
  if (status === 428) {
    return 'The page did not send the version of the settings it was based on, so nothing was saved. '
      + 'Reload and reapply your edits.';
  }
  if (status === 409 && body && body.conflict) {
    return 'Someone else changed these settings after this page loaded, so nothing was saved. '
      + 'Your edits are still here. Reload and reapply them to see the current values with your edits put back.';
  }
  return (body && body.error) || ('Error ' + status);
}

// 143: the Settings page's own state. _cfgPending holds the edits a conflict refused,
// to put back after a reload; _cfgPersonas is the Agents tab's text; _cfgShown says
// whether the page is rendered, so Back between tabs keeps the edits in it.
var _cfgPending = null;
var _cfgPersonas = null;
var _cfgShown = false;

// spec 081-config-ui: full editable config surface (US1 read T018-T019, US2 edit T033).
// Renders all sections, masking secrets and preserving ${VAR} placeholders as
// delivered by /api/config/all. Scalar sections (global) and the orchestration
// catalogs (endpoints / model_endpoints / modes) are editable in-place with
// per-field validation, hot-reload-vs-restart feedback, optimistic-concurrency
// (409) reload prompts, and catalog create/edit/delete with delete-protection.
var CFG_CATALOGS = ['endpoints', 'model_endpoints', 'modes'];
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
var _cfgTab = null;   // active config section id (tab)
function cfgTabId(secId)   { return 'cfg-tab-' + secId; }
function cfgPanelId(secId) { return 'cfg-panel-' + secId; }
var CFG_HINT = 'font-size:11px;color:var(--color-text-muted);margin-top:2px';
var CFG_INPUT = 'width:100%;box-sizing:border-box;background:var(--color-bg-base);border:1px solid var(--color-border);'
  + 'border-radius:4px;color:var(--color-text-primary);font-family:var(--font-mono,monospace);font-size:12px;padding:5px 7px';
var _cfgHash = null;      // optimistic-concurrency baseline (content_hashes.config_yaml)
// 158 (#241): each editing page's config version. Do NOT consolidate these into one
// variable -- see _version_headers in dashboard.py for why that reintroduces the lost
// edit this guards against.
var _symCfgHash = null;
var _personaCfgHash = null;

// A create has no page GET to take a version from -- the symphonies list is drawn
// from the SSE state, not fetched -- so it asks for one.
async function currentConfigVersion() {
  try {
    var r = await fetch('/api/symphonies');
    return r.headers.get('ETag');
  } catch(e) { return null; }
}

// 158: 'you are out of date' is only actionable if it says so. But 409 is not only
// ours -- 'Symphony already exists' is a 409 too, and telling someone to reload over
// a duplicate name would send them chasing a concurrent edit that never happened.
// _version_refusal marks its own refusals with `conflict: true`; everything else
// keeps the server's message.
function versionErrorText(status, body) {
  if (status === 409 || status === 428) return cfgConflictMessage(status, body);
  return (body && body.error) || ('Error ' + status);
}
var _cfgVersion = null;
var _cfgRoutingHash = null;     // optimistic-concurrency baseline (routing.yaml, spec-078)
var _cfgOverrides = {};         // 126: global field key -> symphonies overriding it
var _cfgRoutingAvail = false;   // whether a routing table is mounted+present on this host
var CFG_WIRE = ['openai', 'anthropic'];
var CFG_STRATEGY = ['normalize', 'reroute'];

function cfgFmtValue(v) {
  if (v === null || v === undefined) return '<span style="color:var(--color-text-muted)">(unset)</span>';
  if (Array.isArray(v)) return v.length ? esc(v.join(', ')) : '<span style="color:var(--color-text-muted)">(empty)</span>';
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  return esc(String(v));
}

function cfgBadge(text, color) {
  return '<span style="display:inline-block;font-size:10px;padding:1px 6px;border-radius:8px;margin-left:6px;'
    + 'background:var(--color-bg-base);border:1px solid ' + color + ';color:' + color + '">' + esc(text) + '</span>';
}

function cfgFlash(el, text, kind) {
  if (!el) return;
  var c = {green: 'var(--color-accent-green)', red: 'var(--color-accent-red)',
    orange: 'var(--color-accent-orange,#d29922)', blue: 'var(--color-accent-blue)',
    muted: 'var(--color-text-muted)'}[kind] || 'var(--color-text-muted)';
  el.innerHTML = '';
  el.textContent = text;
  el.style.color = c;
}

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

// A short, self-documenting hint for one catalog option, derived only from
// fields known to exist on the schema (no invented field names): modes show
// their `tool`, model_endpoints show their `endpoint`.
function cfgOptHint(catalog, item) {
  // Key off the stable schema key (last dotted component), falling back to the
  // user-facing label only when s.key is absent — labels can be humanized
  // independently of the schema, so a label-keyed map would silently break.
  var byKey = {};
  (item.settings || []).forEach(function(s) {
    var k = s.key ? String(s.key).split('.').pop() : s.label;
    byKey[k] = s.current_value;
  });
  if (catalog === 'modes' && byKey.tool) return 'tool=' + byKey.tool;
  if (catalog === 'model_endpoints' && byKey.endpoint) return 'endpoint=' + byKey.endpoint;
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

// Build one editable control for a setting. data-orig carries the JSON of the
// loaded value so change detection can skip unchanged fields (a masked, unchanged
// secret therefore never round-trips — the server treats the mask as a no-op too).
function cfgInput(s) {
  var orig = (s.current_value === undefined) ? null : s.current_value;
  // The payload/error identifier is the bare field name. Source it from the
  // stable dotted `key` (e.g. "global.poll_interval_seconds" → "poll_interval_seconds"),
  // not the display `label`, so a future label change can never break saves.
  var field = s.key ? String(s.key).split('.').pop() : s.label;
  var base = 'data-field="' + esc(field) + '" data-type="' + esc(s.type) + '"'
    + ' data-orig="' + esc(JSON.stringify(orig)) + '"'
    + ' data-label="' + esc(s.label || field) + '" data-secret="' + (s.secret ? 'true' : 'false') + '"';
  var refSpec = _CFG_REFS[field];
  if (refSpec) {
    return cfgRefSelect(s, refSpec, base);
  }
  if (s.type === 'bool') {
    return '<input type="checkbox" ' + base + (s.current_value ? ' checked' : '') + '>';
  }
  if (s.type === 'enum' && s.enum) {
    var opts = s.enum.map(function(o) {
      return '<option' + (String(s.current_value) === String(o) ? ' selected' : '') + '>' + esc(o) + '</option>';
    }).join('');
    return '<select ' + base + ' style="' + CFG_INPUT + '">' + opts + '</select>';
  }
  var scalar = (s.current_value === null || s.current_value === undefined) ? '' : String(s.current_value);
  if (s.type === 'int' || s.type === 'float') {
    var step = s.type === 'float' ? ' step="any"' : '';
    return '<input type="number"' + step + ' ' + base + ' value="' + esc(scalar) + '" style="' + CFG_INPUT + '">';
  }
  if (s.type === 'list') {
    var lv = Array.isArray(s.current_value) ? s.current_value.join(', ') : '';
    return '<input type="text" ' + base + ' value="' + esc(lv) + '" placeholder="comma, separated" style="' + CFG_INPUT + '">';
  }
  if (s.type === 'text') {
    return '<textarea ' + base + ' rows="2" style="' + CFG_INPUT + '">' + esc(scalar) + '</textarea>';
  }
  var ph = s.secret ? ' placeholder="leave masked to keep current"' : '';
  return '<input type="text" ' + base + ph + ' value="' + esc(scalar) + '" style="' + CFG_INPUT + '">';
}

// Coerce a control's value back to its typed form for the JSON payload.
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

// Gather only the fields whose value differs from data-orig, scoped to a container.
function cfgCollectChanges(container) {
  var changes = {};
  container.querySelectorAll('[data-field]').forEach(function(el) {
    if (el.closest('[data-create]')) return;
    var coerced = cfgCoerce(el);
    if (JSON.stringify(coerced) !== el.getAttribute('data-orig')) {
      changes[el.getAttribute('data-field')] = coerced;
    }
  });
  return changes;
}

// 143: the saved value of a field, read back from its data attribute.
function cfgParseSaved(text) {
  if (text === null || text === undefined) return null;
  try { return JSON.parse(text); } catch(e) { return text; }
}

// 143: every setting in a container, with its saved value and its value now. A
// create form is left out unless withCreate is set, because it has nothing saved.
function cfgEntries(container, withCreate) {
  var out = [];
  function inScope(el) { return withCreate || !el.closest('[data-create]'); }
  container.querySelectorAll('[data-field]').forEach(function(el) {
    if (!inScope(el)) return;
    var now = cfgCoerce(el);
    var saved = el.getAttribute('data-orig');
    out.push({
      key: el.getAttribute('data-field'),
      label: el.getAttribute('data-label') || el.getAttribute('data-field'),
      before: cfgParseSaved(saved), after: now,
      secret: el.getAttribute('data-secret') === 'true',
      changed: JSON.stringify(now) !== saved,
    });
  });
  container.querySelectorAll('[data-rkey]').forEach(function(el) {
    if (!inScope(el)) return;
    var now = cfgRCoerce(el);
    var saved = el.getAttribute('data-rorig');
    out.push({
      key: el.getAttribute('data-rkey'), label: el.getAttribute('data-rkey'),
      before: cfgParseSaved(saved), after: now, secret: false,
      changed: JSON.stringify(now) !== saved,
    });
  });
  return out;
}

function cfgChanged(entry) { return entry.changed; }

// 143: what a conflict reload puts back: each changed setting's value, by key.
function cfgPendingFor(container, entries) {
  var changes = {};
  entries.forEach(function(e) { changes[e.key] = e.after; });
  return {scope: container.getAttribute('data-scope') || '', changes: changes};
}

// 143: what a create form will add: one row per filled-in field, with secrets hidden.
function cfgNewRows(container) {
  return cfgEntries(container, true).filter(function(e) {
    return e.after !== null && e.after !== '' && !(Array.isArray(e.after) && !e.after.length);
  }).map(function(e) {
    return {label: e.label, before: '(new)', after: e.secret ? '(hidden)' : cfgDiffText(e.after), secret: e.secret};
  });
}

// 143: after a save, what is on screen is what is saved, so the next change is
// measured from here and the unsaved-changes prompt does not fire for it.
function cfgRebaseline(container) {
  container.querySelectorAll('[data-field]').forEach(function(el) {
    if (el.closest('[data-create]')) return;
    el.setAttribute('data-orig', JSON.stringify(cfgCoerce(el)));
  });
  container.querySelectorAll('[data-rkey]').forEach(function(el) {
    if (el.closest('[data-create]')) return;
    el.setAttribute('data-rorig', cfgROrig(cfgRCoerce(el), el.getAttribute('data-rtype')));
  });
}

// 143: the review step. A save first lists each changed setting, old and new, and goes
// ahead only when the reader confirms; "Keep editing" leaves the edits as they are.
// Built from DOM text nodes, so a value is never read as markup.
function cfgShowReview(container, rows, onConfirm) {
  container.querySelectorAll('[data-review]').forEach(function(old) { old.parentNode.removeChild(old); });
  var box = document.createElement('div');
  box.setAttribute('data-review', '');
  box.setAttribute('role', 'region');
  box.setAttribute('aria-label', 'Review your changes');
  box.style.cssText = 'margin-top:10px;padding:10px;border:1px solid var(--color-accent-blue);border-radius:6px;'
    + 'background:var(--color-bg-base);font-size:12px';
  var heading = document.createElement('div');
  heading.style.fontWeight = 'bold';
  heading.textContent = 'Review your changes before saving';
  box.appendChild(heading);
  rows.forEach(function(row) {
    var line = document.createElement('div');
    line.setAttribute('data-review-row', '');
    line.style.margin = '6px 0';
    var name = document.createElement('div');
    name.style.color = 'var(--color-text-muted)';
    name.textContent = row.label;
    var was = document.createElement('div');
    was.style.fontFamily = 'var(--font-mono,monospace)';
    was.style.whiteSpace = 'pre-wrap';
    was.textContent = 'Was: ' + row.before;
    var now = document.createElement('div');
    now.style.fontFamily = 'var(--font-mono,monospace)';
    now.style.whiteSpace = 'pre-wrap';
    now.textContent = 'Now: ' + row.after;
    line.appendChild(name);
    line.appendChild(was);
    line.appendChild(now);
    box.appendChild(line);
  });
  var actions = document.createElement('div');
  actions.style.display = 'flex';
  actions.style.gap = '8px';
  var save = document.createElement('button');
  save.type = 'button';
  save.className = 'cfg-btn';
  // The one filled action on the page, so it carries the success pair the contrast check reads.
  save.style.cssText = 'background:var(--color-btn-success);border:none;color:var(--color-on-action);padding:4px 12px;border-radius:4px;cursor:pointer';
  save.textContent = 'Save these changes';
  save.addEventListener('click', function() {
    if (box.parentNode) box.parentNode.removeChild(box);
    onConfirm();
  });
  var keep = document.createElement('button');
  keep.type = 'button';
  keep.className = 'cfg-btn';
  keep.textContent = 'Keep editing';
  keep.addEventListener('click', function() {
    if (box.parentNode) box.parentNode.removeChild(box);
  });
  actions.appendChild(save);
  actions.appendChild(keep);
  box.appendChild(actions);
  container.appendChild(box);
}

// 143: put a saved value back into its control after a reload.
function cfgSetControl(el, value) {
  if (el.type === 'checkbox') { el.checked = !!value; return; }
  if (Array.isArray(value)) { el.value = value.join(', '); return; }
  el.value = value === null || value === undefined ? '' : String(value);
}

// 143: after "Reload and reapply", the edits a conflict refused are back in their
// form, against the current values, for the reader to check and save again.
function cfgReapplyPending() {
  var pending = _cfgPending;
  if (!pending) return;
  _cfgPending = null;
  var box = null;
  document.querySelectorAll('#config-page-section [data-scope]').forEach(function(el) {
    if (!box && el.getAttribute('data-scope') === pending.scope) box = el;
  });
  if (!box) return;
  box.querySelectorAll('[data-field], [data-rkey]').forEach(function(el) {
    var key = el.getAttribute('data-field') || el.getAttribute('data-rkey');
    if (Object.prototype.hasOwnProperty.call(pending.changes, key)) {
      cfgSetControl(el, pending.changes[key]);
    }
  });
  cfgFlash(box.querySelector('[data-status]'), 'Your edits are back in the form. Check them, then save again.', 'blue');
}

// 143: the Settings page's unsaved edits. Editors stay in the page while a tab is
// open, so switching tabs keeps them; leaving the page does not.
function cfgOnSettingsPage() {
  var el = document.getElementById('config-page');
  return !!el && el.style.display !== 'none';
}

function cfgHasUnsaved() {
  var root = document.getElementById('config-page-section');
  if (!root || !cfgOnSettingsPage()) return false;
  return cfgEntries(root).some(cfgChanged);
}

// 143: may the reader leave for nextPath? Only the unsaved-changes prompt can say no.
function cfgLeaveAllowed(nextPath) {
  if (!cfgHasUnsaved()) return true;
  if (String(nextPath || '').indexOf('/config') === 0) return true;
  return window.confirm('You have unsaved changes on this page. Leave without saving?');
}

window.addEventListener('beforeunload', function(event) {
  if (!cfgHasUnsaved()) return;
  event.preventDefault();
  event.returnValue = '';
});

// 143: the "Used by" line in words. A stage reads as its stage name.
function cfgUsedByLabel(use) {
  var name = String(use.name || '');
  if (use.kind === 'stage') return stageLabel(name) + ' stage';
  if (use.kind === 'symphony') return 'symphony ' + name;
  if (use.kind === 'mode') return 'mode ' + name;
  if (use.kind === 'model') return 'model ' + name;
  if (use.kind === 'model_server') return 'model server ' + name;
  return name;
}

function cfgUsedByHtml(list) {
  var names = (list || []).map(cfgUsedByLabel);
  return '<div data-used-by style="' + CFG_HINT + ';margin-top:6px">Used by: '
    + (names.length ? esc(names.join(', ')) : 'Not used') + '</div>';
}

// 143: why a setting cannot be edited here, in plain words.
function cfgReadOnlyNote(s, catalog) {
  if (catalog === 'symphonies') return "Edit this on the symphony's own page.";
  if (s.invalid) return 'The config file holds a value that fails validation, so it is read-only here. Fix it in the file.';
  if (s.is_env_placeholder) return 'This value comes from an environment variable, so it is read-only here.';
  return 'Read-only here.';
}

function cfgClearErrors(container) {
  container.querySelectorAll('[data-err]').forEach(function(slot) {
    slot.textContent = '';
    slot.style.display = 'none';
  });
}

function cfgApplyErrors(container, errs) {
  (errs || []).forEach(function(e) {
    if (!e.key) return;
    var f = container.querySelector('[data-field="' + e.key + '"]');
    var holder = f ? f.closest('[data-fieldrow]') : null;
    var slot = holder ? holder.querySelector('[data-err]') : null;
    if (slot) { slot.textContent = e.message; slot.style.display = ''; }
  });
}

// Optimistic-concurrency conflict: the file changed under us. Offer a reload so
// the operator picks up the latest baseline before re-applying their edit.
function cfgConflict(statusEl, status, body) {
  if (!statusEl) return;
  statusEl.innerHTML = '';
  var text = document.createElement('span');
  text.style.color = 'var(--color-accent-orange,#d29922)';
  text.textContent = cfgConflictMessage(status, body) + ' ';
  statusEl.appendChild(text);
  var btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'cfg-btn';
  btn.textContent = 'Reload and reapply';
  btn.addEventListener('click', function() { loadConfigPage(); });
  statusEl.appendChild(btn);
}

// Map a save response onto field errors + a status badge. Returns true on success.
// `store` selects which optimistic-concurrency baseline the returned new_hash
// refreshes: 'routing_yaml' → _cfgRoutingHash, otherwise the config.yaml baseline.
function cfgHandleSave(status, body, container, statusEl, store, newHash) {
  cfgClearErrors(container);
  // Persona writes answer 204 with the new version in the ETag header.
  var saved = store === 'persona' ? (status === 200 || status === 204) : (status === 200 && body.ok);
  if (saved) {
    if (store === 'persona') {
      if (newHash) _personaCfgHash = newHash;
    } else if (body.new_hash) {
      if (store === 'routing_yaml') _cfgRoutingHash = body.new_hash;
      else _cfgHash = body.new_hash;
    }
    _cfgPending = null;
    cfgRebaseline(container);
    if (body.applied === 'hot_reloaded') cfgFlash(statusEl, 'Saved · applied live', 'green');
    else if (body.applied === 'staged_restart') cfgFlash(statusEl, body.message || 'Saved · restart required to apply', 'orange');
    else if (body.applied === 'staged_next_job') cfgFlash(statusEl, 'Saved · applies on next job', 'blue');
    else cfgFlash(statusEl, 'Saved', 'green');
    // Reload for the server's own values, unless another editor on the page holds
    // edits that a reload would drop.
    if (!cfgHasUnsaved()) setTimeout(loadConfigPage, 1000);
    return true;
  }
  if (status === 409 || status === 428) { cfgConflict(statusEl, status, body); return false; }
  var errs = body.errors || [];
  cfgApplyErrors(container, errs);
  var general = errs.filter(function(e) { return !e.key; }).map(function(e) { return e.message; }).join('; ');
  cfgFlash(statusEl, general || body.message || ('Error ' + status), 'red');
  return false;
}

async function cfgPut(url, payload, container, statusEl, btn) {
  // Fail closed, as the old Global Config page did: without a version to write
  // against, the server would refuse anyway, and saying so here explains what to do.
  if (payload.body && Object.prototype.hasOwnProperty.call(payload.body, 'base_hash') && !payload.body.base_hash) {
    cfgFlash(statusEl, 'Cannot save: this page did not load a configuration version. Reload the page and try again.', 'red');
    return;
  }
  if (btn) btn.disabled = true;
  try {
    var headers = {'Content-Type': 'application/json'};
    if (payload['If-Match'] !== undefined) headers['If-Match'] = payload['If-Match'];
    var r = await fetch(url, {method: payload._method || 'PUT', headers: headers,
      body: JSON.stringify(payload.body)});
    var body = {};
    try { body = await r.json(); } catch(e) { body = {}; }
    if (payload.reload && r.ok) loadConfigPage();
    else cfgHandleSave(r.status, body, container, statusEl, payload.store, r.headers.get('ETag'));
  } catch(e) {
    cfgFlash(statusEl, 'Network error', 'red');
  }
  if (btn) btn.disabled = false;
}

function cfgSaveSection(btn, sectionId) {
  var container = btn.closest('[data-section]');
  var statusEl = container.querySelector('[data-status]');
  cfgClearErrors(container);
  var entries = cfgEntries(container).filter(cfgChanged);
  if (!entries.length) { cfgFlash(statusEl, 'No changes to save', 'muted'); return; }
  cfgShowReview(container, cfgDiffRows(entries), function() {
    var changes = cfgCollectChanges(container);
    _cfgPending = cfgPendingFor(container, entries);
    cfgPut('/api/config/section/' + encodeURIComponent(sectionId),
      {body: {store: 'config_yaml', section: sectionId, changes: changes, base_hash: _cfgHash}},
      container, statusEl, btn);
  });
}

function cfgSaveItem(btn, catalog, itemId) {
  var container = btn.closest('[data-item]');
  var statusEl = container.querySelector('[data-status]');
  cfgClearErrors(container);
  var entries = cfgEntries(container).filter(cfgChanged);
  if (!entries.length) { cfgFlash(statusEl, 'No changes to save', 'muted'); return; }
  cfgShowReview(container, cfgDiffRows(entries), function() {
    var changes = cfgCollectChanges(container);
    _cfgPending = cfgPendingFor(container, entries);
    if (catalog === 'personas') {
      // Persona text goes to the personas API, versioned by its ETag like the config.
      cfgPut('/api/personas/' + encodeURIComponent(itemId),
        {store: 'persona', 'If-Match': _personaCfgHash || '', body: {instructions: changes.instructions}},
        container, statusEl, btn);
      return;
    }
    cfgPut('/api/config/catalog/' + encodeURIComponent(catalog) + '/' + encodeURIComponent(itemId),
      {body: {changes: changes, base_hash: _cfgHash}}, container, statusEl, btn);
  });
}

function cfgDeleteItem(btn, catalog, itemId) {
  var container = btn.closest('[data-item]');
  var statusEl = container.querySelector('[data-status]');
  var rows = [{label: 'Delete this entry', before: itemId, after: '(deleted)', secret: false}];
  cfgShowReview(container, rows, function() {
    cfgPut('/api/config/catalog/' + encodeURIComponent(catalog) + '/' + encodeURIComponent(itemId),
      {_method: 'DELETE', body: {base_hash: _cfgHash}}, container, statusEl, btn);
  });
}

function cfgToggleAdd(catalog) {
  var form = document.getElementById('cfg-add-' + catalog);
  if (form) form.style.display = (form.style.display === 'none' || !form.style.display) ? '' : 'none';
}

function cfgCreateItem(btn, catalog) {
  var container = btn.closest('[data-item]');
  var statusEl = container.querySelector('[data-status]');
  cfgClearErrors(container);
  var item = {};
  cfgEntries(container, true).forEach(function(e) {
    var v = e.after;
    if (v !== null && v !== '' && !(Array.isArray(v) && !v.length)) item[e.key] = v;
  });
  if (!item.name) { cfgFlash(statusEl, 'A name is required', 'red'); return; }
  cfgShowReview(container, cfgNewRows(container), function() {
    cfgPut('/api/config/catalog/' + encodeURIComponent(catalog),
      {_method: 'POST', body: {item: item, base_hash: _cfgHash}}, container, statusEl, btn);
  });
}

// spec 081-config-ui US3 (T041): routing-table CRUD wired to /api/config/routing
// (NOT the catalog endpoints). Routing entries carry a nested `target`; edits are
// staged for the next performer job (no live reload) — surfaced as a blue label.
function cfgROrig(value, type) {
  if (type === 'list') return JSON.stringify(Array.isArray(value) ? value : []);
  return JSON.stringify(value == null ? '' : String(value));
}
function cfgRText(rkey, value, type, ph) {
  var v = (value == null) ? '' : (Array.isArray(value) ? value.join(', ') : String(value));
  return '<input type="text" data-rkey="' + esc(rkey) + '" data-rtype="' + esc(type) + '"'
    + ' data-rorig="' + esc(cfgROrig(value, type)) + '" value="' + esc(v) + '"'
    + (ph ? ' placeholder="' + esc(ph) + '"' : '') + ' style="' + CFG_INPUT + '">';
}
function cfgRSelect(rkey, value, opts) {
  var o = opts.map(function(x) {
    return '<option' + (String(value) === String(x) ? ' selected' : '') + '>' + esc(x) + '</option>';
  }).join('');
  return '<select data-rkey="' + esc(rkey) + '" data-rtype="enum" data-rorig="' + esc(cfgROrig(value, 'enum'))
    + '" style="' + CFG_INPUT + '">' + o + '</select>';
}
function cfgRCoerce(el) {
  var v = el.value;
  if (el.getAttribute('data-rtype') === 'list')
    return v.split(',').map(function(x) { return x.trim(); }).filter(function(x) { return x.length; });
  return v;
}
function cfgRRow(label, control) {
  return '<div data-fieldrow style="padding:5px 0">'
    + '<label style="font-size:11px;color:var(--color-text-muted);display:block;margin-bottom:2px">' + esc(label) + '</label>'
    + control + '</div>';
}
function cfgRoutingFields(e) {
  var t = e.target || {};
  return cfgRRow('backend', cfgRText('backend', e.backend, 'string'))
    + cfgRRow('model', cfgRText('model', e.model, 'string'))
    + cfgRRow('target.base_url', cfgRText('target.base_url', t.base_url, 'string'))
    + cfgRRow('target.wire_format', cfgRSelect('target.wire_format', t.wire_format || 'openai', CFG_WIRE))
    + cfgRRow('target.strategy', cfgRSelect('target.strategy', t.strategy || 'normalize', CFG_STRATEGY))
    + cfgRRow('target.normalizers', cfgRText('target.normalizers', t.normalizers || [], 'list', 'harmony_tool_calls, strip_reasoning'))
    + cfgRRow('target.reroute_upstream', cfgRText('target.reroute_upstream', t.reroute_upstream, 'string', 'only for reroute strategy'));
}
function cfgRoutingEntryBlock(entry, index) {
  return '<div data-rentry data-scope="routing:' + index + '" style="background:var(--color-bg-base);border:1px solid var(--color-border);border-radius:6px;padding:10px;margin-bottom:10px">'
    + '<div style="font-weight:bold;color:var(--color-text-primary);font-size:13px;margin-bottom:4px">'
    + esc(entry.backend || '') + ' / ' + esc(entry.model || '') + '</div>'
    + cfgRoutingFields(entry)
    + cfgUsedByHtml(entry.used_by)
    + '<div style="display:flex;gap:8px;align-items:center;margin-top:8px">'
    + '<button type="button" class="cfg-btn" onclick="cfgSaveRoutingEntry(this,' + index + ')">Save</button>'
    + '<button type="button" class="cfg-btn cfg-btn-danger" onclick="cfgDeleteRoutingEntry(this,' + index + ')">Delete</button>'
    + '<span data-status style="font-size:11px;margin-left:6px"></span></div></div>';
}
function cfgRoutingAddBlock() {
  return '<div id="cfg-add-routing" data-rentry data-create style="display:none;background:var(--color-bg-base);'
    + 'border:1px dashed var(--color-accent-blue);border-radius:6px;padding:10px;margin-bottom:10px">'
    + '<div style="font-weight:bold;color:var(--color-accent-blue);font-size:13px;margin-bottom:4px">New routing entry</div>'
    + cfgRoutingFields({})
    + '<div style="display:flex;gap:8px;align-items:center;margin-top:8px">'
    + '<button type="button" class="cfg-btn" onclick="cfgCreateRoutingEntry(this)">Create</button>'
    + '<span data-status style="font-size:11px;margin-left:6px"></span></div></div>';
}
function cfgCollectRoutingChanges(container, all) {
  var changes = {}, target = {};
  container.querySelectorAll('[data-rkey]').forEach(function(el) {
    var v = cfgRCoerce(el);
    if (!all && JSON.stringify(v) === el.getAttribute('data-rorig')) return;
    if (all && (v === '' || (Array.isArray(v) && !v.length))) return;
    var key = el.getAttribute('data-rkey');
    if (key.indexOf('target.') === 0) target[key.slice(7)] = v;
    else changes[key] = v;
  });
  if (Object.keys(target).length) changes.target = target;
  return changes;
}
function cfgSaveRoutingEntry(btn, index) {
  var container = btn.closest('[data-rentry]');
  var statusEl = container.querySelector('[data-status]');
  var entries = cfgEntries(container).filter(cfgChanged);
  if (!entries.length) { cfgFlash(statusEl, 'No changes to save', 'muted'); return; }
  cfgShowReview(container, cfgDiffRows(entries), function() {
    var changes = cfgCollectRoutingChanges(container, false);
    _cfgPending = cfgPendingFor(container, entries);
    cfgPut('/api/config/routing/entry/' + index,
      {store: 'routing_yaml', body: {changes: changes, base_hash: _cfgRoutingHash}}, container, statusEl, btn);
  });
}
function cfgDeleteRoutingEntry(btn, index) {
  var container = btn.closest('[data-rentry]');
  var statusEl = container.querySelector('[data-status]');
  var rows = [{label: 'This routing entry', before: 'present', after: '(deleted)', secret: false}];
  cfgShowReview(container, rows, function() {
    cfgPut('/api/config/routing/entry/' + index,
      {store: 'routing_yaml', _method: 'DELETE', body: {base_hash: _cfgRoutingHash}}, container, statusEl, btn);
  });
}
function cfgCreateRoutingEntry(btn) {
  var container = btn.closest('[data-rentry]');
  var statusEl = container.querySelector('[data-status]');
  var entry = cfgCollectRoutingChanges(container, true);
  if (!entry.backend || !entry.model) { cfgFlash(statusEl, 'backend and model are required', 'red'); return; }
  cfgShowReview(container, cfgNewRows(container), function() {
    var fresh = cfgCollectRoutingChanges(container, true);
    cfgPut('/api/config/routing/entry',
      {store: 'routing_yaml', _method: 'POST', body: {entry: fresh, base_hash: _cfgRoutingHash}}, container, statusEl, btn);
  });
}
function cfgRoutingSection(sec, banner) {
  var body;
  if (_cfgRoutingAvail) {
    body = '<div style="font-size:11px;color:var(--color-accent-blue);margin-bottom:8px">'
      + 'Routing edits apply to the next performer job (not live).</div>'
      + '<div id="cfg-routing-entries"><span class="empty-state">Loading routing…</span></div>'
      + cfgRoutingAddBlock()
      + '<div style="margin-top:6px"><button type="button" class="cfg-btn" onclick="cfgToggleAdd(&quot;routing&quot;)">+ Add routing entry</button></div>';
  } else {
    body = '<div class="empty-state">Routing is read-only on this host.</div>';
  }
  var desc = sec.description ? '<div style="font-size:12px;color:var(--color-text-muted);margin-bottom:8px">' + esc(sec.description) + '</div>' : '';
  return '<section data-section="routing" aria-label="' + esc(sec.title)
    + '" style="background:var(--color-bg-surface);border:1px solid var(--color-border);border-radius:6px;padding:14px;margin-bottom:14px">'
    + '<h3 style="margin:0 0 8px 0;font-size:14px;color:var(--color-text-primary)">' + esc(sec.title) + '</h3>'
    + desc + banner + body + '</section>';
}
async function loadRoutingEntries() {
  var host = document.getElementById('cfg-routing-entries');
  if (!host) return;
  try {
    var r = await fetch('/api/config/routing');
    var d = await r.json();
    _cfgRoutingHash = d.content_hash || null;
    var entries = d.entries || [];
    host.innerHTML = entries.length
      ? entries.map(function(e, i) { return cfgRoutingEntryBlock(e, i); }).join('')
      : '<div class="empty-state">No routing entries yet.</div>';
  } catch(e) {
    host.innerHTML = '<div class="empty-state">Failed to load routing entries</div>';
  }
  cfgReapplyPending();
}

// 126: say which symphonies set their own value for a global field, and link
// to each, so a reader can tell which value is in effect where.
function cfgOverrideNote(key) {
  var list = (_cfgOverrides && Object.prototype.hasOwnProperty.call(_cfgOverrides, key)) ? _cfgOverrides[key] : null;
  if (!Array.isArray(list) || list.length === 0) return '';
  var links = list.map(function(o) {
    var name = String(o.symphony || '');
    return '<a href="/symphonies/' + encodeURIComponent(name) + '">' + esc(name) + '</a>'
      + (o.value ? ' (' + esc(String(o.value)) + ')' : '');
  }).join(', ');
  return '<div data-override-note style="' + CFG_HINT + ';color:var(--color-accent-yellow)">'
    + 'Overridden by ' + (list.length === 1 ? 'symphony ' : 'symphonies ') + links
    + '. The value here applies only to a symphony that does not set its own.</div>';
}

// 126: the status line in plain words, not a bare version number and a flag.
function cfgStatusLine(data) {
  var reloads = Number(data.config_version) || 0;
  var loaded = reloads === 0
    ? 'Loaded when the daemon started; not reloaded since.'
    : 'Reloaded ' + reloads + (reloads === 1 ? ' time' : ' times') + ' since the daemon started.';
  var routing = data.routing_available
    ? 'A model routing table is mounted and can be edited under Routing.'
    : (data.routing_mounted
      ? 'A performer endpoint mounts a routing table, but its file is missing on this host, so model routing uses defaults.'
      : 'No routing table is mounted on this host, so model routing uses defaults.');
  return loaded + ' ' + routing;
}

function cfgFieldRow(s, showOverrides, note) {
  var meta = [];
  meta.push(cfgBadge(s.type, 'var(--color-border)'));
  if (s.secret) meta.push(cfgBadge('secret', 'var(--color-accent-orange,#d29922)'));
  if (s.is_env_placeholder) meta.push(cfgBadge('env', 'var(--color-accent-blue)'));
  if (s.restart_required) meta.push(cfgBadge('restart required', 'var(--color-accent-orange,#d29922)'));
  if (!s.editable) meta.push(cfgBadge('read-only', 'var(--color-text-muted)'));
  if (s.invalid) meta.push(cfgBadge('invalid on disk', 'var(--color-accent-red)'));
  var control = s.editable ? cfgInput(s)
    : '<div style="font-family:var(--font-mono,monospace);font-size:12px;color:'
      + (s.invalid ? 'var(--color-accent-red)' : 'var(--color-text-primary)') + '">' + cfgFmtValue(s.current_value) + '</div>';
  var constraint = '';
  if (s.enum && s.enum.length) constraint = 'one of: ' + esc(s.enum.join(', '));
  else if (s.range) {
    // The descriptor layer emits either {min,max} (numbers) or
    // {min_length,max_length} (strings/collections). Handle both shapes, plus
    // partial bounds (only-min or only-max), so we never render `undefined`.
    var r = s.range;
    var hasLen = (r.min_length !== null && r.min_length !== undefined)
              || (r.max_length !== null && r.max_length !== undefined);
    var noun = hasLen ? 'length' : 'range';
    var lo = hasLen ? r.min_length : r.min;
    var hi = hasLen ? r.max_length : r.max;
    var hasLo = (lo !== null && lo !== undefined);
    var hasHi = (hi !== null && hi !== undefined);
    // gt/lt are exclusive, so render > / < instead of ≥ / ≤ so the displayed range
    // matches server validation. Length bounds are always inclusive.
    var loSym = (!hasLen && r.min_exclusive) ? '> ' : '≥ ';
    var hiSym = (!hasLen && r.max_exclusive) ? '< ' : '≤ ';
    if (hasLo && hasHi) {
      if (!hasLen && (r.min_exclusive || r.max_exclusive))
        constraint = noun + ': ' + loSym + esc(String(lo)) + ' to ' + hiSym + esc(String(hi));
      else
        constraint = noun + ': ' + esc(String(lo)) + ' to ' + esc(String(hi));
    }
    else if (hasLo) constraint = noun + ': ' + loSym + esc(String(lo));
    else if (hasHi) constraint = noun + ': ' + hiSym + esc(String(hi));
  }
  var help = s.help ? '<div style="' + CFG_HINT + '">' + esc(s.help) + '</div>' : '';
  var ch = constraint ? '<div style="' + CFG_HINT + '">' + constraint + '</div>' : '';
  var def = (s.default !== null && s.default !== undefined)
    ? '<div style="' + CFG_HINT + '">default: ' + cfgFmtValue(s.default) + '</div>' : '';
  var over = showOverrides ? cfgOverrideNote(s.key) : '';
  // 143: a read-only setting says why, in words, rather than only showing a badge.
  var why = s.editable ? '' : '<div data-readonly-note style="' + CFG_HINT + '">'
    + esc(note || cfgReadOnlyNote(s, '')) + '</div>';
  return '<div data-fieldrow style="padding:8px 0;border-bottom:1px solid var(--color-bg-elevated)">'
    + '<div style="display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap">'
    + '<label style="font-size:12px;color:var(--color-text-primary);font-weight:600">' + esc(s.label || s.key) + '</label>'
    + '<div style="text-align:right">' + meta.join('') + '</div></div>'
    + '<div style="margin-top:3px">' + control + '</div>'
    + help + ch + def + over + why
    + '<div data-err style="display:none;font-size:11px;color:var(--color-accent-red);margin-top:3px"></div>'
    + '</div>';
}

// Keep the legacy name as an alias so existing callers/tests resolve.
function cfgSettingRow(s) { return cfgFieldRow(s); }

function cfgItemBlock(item, catalog, editable) {
  // A view-only item has no Save action, so it must never offer an edit affordance:
  // every field is forced read-only, even individually-editable ones, and the reason
  // is given. Use a shallow copy so the source setting object is never mutated.
  var rows = (item.settings || []).map(function(s) {
    return cfgFieldRow(editable ? s : Object.assign({}, s, {editable: false}), false, cfgReadOnlyNote(s, catalog));
  }).join('');
  var footer = '';
  if (editable) {
    var iid = esc(item.id);
    var cat = esc(catalog);
    var delTitle = item.deletable ? ''
      : ' title="Protected: remove the references first. Referenced by: ' + esc((item.referenced_by || []).join(', ')) + '"';
    var delBtn = '<button type="button" class="cfg-btn cfg-btn-danger"' + (item.deletable ? '' : ' disabled')
      + delTitle + ' onclick="cfgDeleteItem(this, &quot;' + cat + '&quot;, &quot;' + iid + '&quot;)">Delete</button>';
    footer = cfgUsedByHtml(item.used_by)
      + '<div style="display:flex;gap:8px;align-items:center;margin-top:8px">'
      + '<button type="button" class="cfg-btn" onclick="cfgSaveItem(this, &quot;' + cat + '&quot;, &quot;' + iid + '&quot;)">Save</button>'
      + delBtn
      + '<span data-status style="font-size:11px;margin-left:6px"></span></div>';
  } else if (catalog === 'symphonies') {
    // 143: a symphony's settings are edited on its page. This is the link to it.
    var href = '/symphonies/' + encodeURIComponent(item.id);
    footer = '<div style="margin-top:8px"><a href="' + esc(href) + '" onclick="navigate(event,&quot;' + esc(href)
      + '&quot;)">Edit settings</a></div>';
  } else {
    footer = cfgUsedByHtml(item.used_by);
  }
  return '<div data-item data-scope="' + esc('item:' + catalog + '/' + item.id)
    + '" style="background:var(--color-bg-base);border:1px solid var(--color-border);border-radius:6px;padding:10px;margin-bottom:10px">'
    + '<div style="font-weight:bold;color:var(--color-text-primary);font-size:13px;margin-bottom:4px">' + esc(item.id) + '</div>'
    + (rows || '<div class="empty-state">No editable fields.</div>')
    + footer
    + '</div>';
}

// 143: one persona in the Agents tab. Its text is the only editable part. Save and
// Reset go through the same review and conflict handling as every other editor.
function cfgPersonaBlock(p, usedBy) {
  var role = String(p.role || '');
  var text = String(p.instructions || '');
  return '<div data-item data-scope="' + esc('item:personas/' + role)
    + '" style="background:var(--color-bg-base);border:1px solid var(--color-border);border-radius:6px;padding:10px;margin-bottom:10px">'
    + '<div style="font-weight:bold;color:var(--color-text-primary);font-size:13px;margin-bottom:4px">'
    + esc(agentLabel(role)) + personaStageNote(role) + '</div>'
    + '<div style="' + CFG_HINT + '">' + (p.is_default ? 'Using the default text.' : 'Custom text.') + '</div>'
    + '<textarea data-field="instructions" data-label="Instructions" data-type="text"'
    + ' data-orig="' + esc(JSON.stringify(text)) + '" rows="8" style="' + CFG_INPUT + ';margin-top:6px">'
    + esc(text) + '</textarea>'
    + cfgUsedByHtml(usedBy)
    + '<div style="display:flex;gap:8px;align-items:center;margin-top:8px">'
    + '<button type="button" class="cfg-btn" onclick="cfgSaveItem(this, &quot;personas&quot;, &quot;' + esc(role) + '&quot;)">Save</button>'
    + '<button type="button" class="cfg-btn" onclick="cfgResetPersona(this, &quot;' + esc(role) + '&quot;)">Reset to default</button>'
    + '<span data-status style="font-size:11px;margin-left:6px"></span></div></div>';
}

function cfgResetPersona(btn, role) {
  var container = btn.closest('[data-item]');
  var statusEl = container.querySelector('[data-status]');
  if (!confirm('Reset the ' + role + ' instructions to the default text?')) return;
  cfgPut('/api/personas/' + encodeURIComponent(role),
    {store: 'persona', _method: 'DELETE', 'If-Match': _personaCfgHash || '', reload: true},
    container, statusEl, btn);
}

// 143: the Agents tab. Persona text comes from /api/personas (the text in effect, with
// defaults), and each persona's "Used by" comes from the config's descriptor.
function cfgPersonasSection(sec, banner) {
  var usage = {};
  (sec.items || []).forEach(function(it) { usage[it.id] = it.used_by || []; });
  var body = _cfgPersonas
    ? _cfgPersonas.map(function(p) { return cfgPersonaBlock(p, usage[String(p.role || '')] || []); }).join('')
    : '<div class="empty-state">Could not load the agents\' instructions. Reload the page to try again.</div>';
  var desc = sec.description ? '<div style="font-size:12px;color:var(--color-text-muted);margin-bottom:8px">' + esc(sec.description) + '</div>' : '';
  return '<section data-section="personas" data-scope="section:personas" aria-label="' + esc(sec.title)
    + '" style="background:var(--color-bg-surface);border:1px solid var(--color-border);border-radius:6px;padding:14px;margin-bottom:14px">'
    + '<h3 style="margin:0 0 8px 0;font-size:14px;color:var(--color-text-primary)">' + esc(sec.title) + '</h3>'
    + desc + banner + body + '</section>';
}

// A blank create form for a catalog, seeded from the field shape of an existing
// item (or an empty name field when the catalog has no entries yet).
function cfgAddBlock(sec) {
  var template = (sec.items && sec.items.length) ? sec.items[0].settings || [] : [];
  var blanks = template.map(function(s) {
    var b = {key: s.key, label: s.label, help: s.help, type: s.type, enum: s.enum, range: s.range,
      secret: s.secret, editable: true, current_value: null};
    return cfgFieldRow(b);
  }).join('');
  if (!blanks) {
    blanks = cfgFieldRow({key: 'name', label: 'name', type: 'string', editable: true, current_value: null});
  }
  return '<div id="cfg-add-' + esc(sec.id) + '" data-item data-create style="display:none;background:var(--color-bg-base);'
    + 'border:1px dashed var(--color-accent-blue);border-radius:6px;padding:10px;margin-bottom:10px">'
    + '<div style="font-weight:bold;color:var(--color-accent-blue);font-size:13px;margin-bottom:4px">New ' + esc(sec.id) + ' entry</div>'
    + blanks
    + '<div style="display:flex;gap:8px;align-items:center;margin-top:8px">'
    + '<button type="button" class="cfg-btn" onclick="cfgCreateItem(this, &quot;' + esc(sec.id) + '&quot;)">Create</button>'
    + '<span data-status style="font-size:11px;margin-left:6px"></span></div>'
    + '</div>';
}

function cfgSectionBlock(sec) {
  var editable = (sec.kind === 'scalar_group') || (sec.kind === 'collection' && CFG_CATALOGS.indexOf(sec.id) !== -1);
  var banner = sec.invalid_banner
    ? '<div role="status" style="background:var(--color-bg-base);border:1px solid var(--color-accent-orange,#d29922);'
      + 'border-radius:4px;padding:8px 10px;margin-bottom:10px;font-size:12px;color:var(--color-accent-orange,#d29922)">'
      + esc(sec.invalid_banner) + '</div>'
    : '';
  if (sec.id === 'routing') return cfgRoutingSection(sec, banner);
  if (sec.id === 'personas') return cfgPersonasSection(sec, banner);
  var body, footer = '';
  if (sec.kind === 'scalar_group') {
    // 126: only the global group can be overridden by a symphony.
    body = (sec.settings || []).map(function(s) { return cfgFieldRow(s, true); }).join('') || '<div class="empty-state">No settings.</div>';
    if (editable) {
      footer = '<div style="display:flex;gap:8px;align-items:center;margin-top:10px">'
        + '<button type="button" class="cfg-btn" onclick="cfgSaveSection(this, &quot;' + esc(sec.id) + '&quot;)">Save changes</button>'
        + '<span data-status style="font-size:11px;margin-left:6px"></span></div>';
    }
  } else {
    body = (sec.items && sec.items.length)
      ? sec.items.map(function(it) { return cfgItemBlock(it, sec.id, editable); }).join('')
      : '<div class="empty-state">No entries.</div>';
    if (editable) {
      body += cfgAddBlock(sec);
      footer = '<div style="margin-top:6px"><button type="button" class="cfg-btn" onclick="cfgToggleAdd(&quot;'
        + esc(sec.id) + '&quot;)">+ Add entry</button></div>';
    }
  }
  var desc = sec.description ? '<div style="font-size:12px;color:var(--color-text-muted);margin-bottom:8px">' + esc(sec.description) + '</div>' : '';
  return '<section data-section="' + esc(sec.id) + '" data-scope="' + esc('section:' + sec.id) + '" aria-label="' + esc(sec.title)
    + '" style="background:var(--color-bg-surface);border:1px solid var(--color-border);border-radius:6px;padding:14px;margin-bottom:14px">'
    + '<h3 style="margin:0 0 8px 0;font-size:14px;color:var(--color-text-primary)">' + esc(sec.title) + '</h3>'
    + desc + banner + body + footer
    + '</section>';
}

// Render the horizontal tab strip (.cfg-tabs, .cfg-tab in dashboard.html). WAI-ARIA tabs
// pattern: roving tabindex, aria-selected/aria-controls on each tab.
function cfgTabBar(sections, active) {
  var tabs = (sections || []).map(function(sec) {
    var on = sec.id === active;
    return '<button type="button" role="tab" id="' + cfgTabId(sec.id) + '"'
      + ' aria-selected="' + (on ? 'true' : 'false') + '"'
      + ' aria-controls="' + cfgPanelId(sec.id) + '"'
      + ' tabindex="' + (on ? '0' : '-1') + '"'
      + ' class="cfg-tab' + (on ? ' active' : '') + '"'
      + ' onclick="cfgSelectTab(&quot;' + esc(sec.id) + '&quot;)"'
      + ' onkeydown="cfgTabKey(event, &quot;' + esc(sec.id) + '&quot;)">'
      + esc(sec.title) + '</button>';
  }).join('');
  return '<div class="cfg-tabs" role="tablist" aria-label="Configuration sections">'
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
  // The hash may carry an item after the tab ("#personas/implementer"), so compare the tab only.
  var current = (location.hash || '').replace(/^#/, '').split('/')[0];
  if (current !== secId) {
    history.pushState(null, '', '/config#' + secId);
  }
}

// 143: a deep link names one item ("#personas/implementer"): scroll to it and focus its first control.
function cfgFocusItem(path) {
  var box = null;
  document.querySelectorAll('#config-page-section [data-scope]').forEach(function(el) {
    if (!box && el.getAttribute('data-scope') === 'item:' + path) box = el;
  });
  if (!box) return;
  if (box.scrollIntoView) box.scrollIntoView({block: 'start'});
  var field = box.querySelector('textarea, input, select, button');
  if (field && field.focus) field.focus();
}

// 143: Back and Forward between tabs of a page already shown only switch the tab.
// The editors, and any unsaved edits in them, are kept.
function cfgSyncTab() {
  var parts = (location.hash || '').replace(/^#/, '').split('/');
  if (parts[0] && document.getElementById(cfgTabId(parts[0]))) cfgSelectTab(parts[0]);
  if (parts.length > 1) cfgFocusItem(parts.join('/'));
}

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

async function loadConfigPage() {
  var el = document.getElementById('config-page-section');
  el.innerHTML = '<span class="empty-state">Loading...</span>';
  var res, data;
  try {
    res = await fetch('/api/config/all');
    data = await res.json();
  } catch(e) {
    el.innerHTML = '<div class="empty-state">Failed to load configuration</div>';
    return;
  }
  if (!res.ok) {
    el.innerHTML = '<div class="empty-state">' + esc(data.error || 'Error loading configuration') + '</div>';
    return;
  }
  _cfgHash = (data.content_hashes || {}).config_yaml || null;
  _cfgVersion = data.config_version;
  _cfgRoutingAvail = !!data.routing_available;
  _cfgOptions = cfgBuildOptions(data.sections);
  _cfgOverrides = data.symphony_overrides || {};
  // 143: the Agents tab edits persona text, versioned by the personas API's ETag.
  try {
    var pres = await fetch('/api/personas');
    _personaCfgHash = pres.ok ? pres.headers.get('ETag') : null;
    _cfgPersonas = pres.ok ? await pres.json() : null;
  } catch(e) {
    _personaCfgHash = null;
    _cfgPersonas = null;
  }
  var header = '<div id="cfg-status-line" style="font-size:11px;color:var(--color-text-muted);margin-bottom:12px">'
    + esc(cfgStatusLine(data))
    + ' <a href="https://github.com/ViviDynamics/coordinare/blob/main/docs/onboarding/05-configuration.md" target="_blank" rel="noopener">How configuration is resolved</a>'
    + '</div>';
  var sections = data.sections || [];
  // The hash names a tab, and may name an item in it: "#personas/implementer".
  var parts = (location.hash || '').replace(/^#/, '').split('/');
  var active = sections.some(function(s) { return s.id === parts[0]; })
    ? parts[0]
    : (sections[0] ? sections[0].id : null);
  _cfgTab = active;
  var panels = sections.map(function(sec) {
    return '<div role="tabpanel" id="' + cfgPanelId(sec.id) + '"'
      + ' aria-labelledby="' + cfgTabId(sec.id) + '"'
      + (sec.id === active ? '' : ' hidden') + '>'
      + cfgSectionBlock(sec) + '</div>';
  }).join('');
  el.innerHTML = header + cfgTabBar(sections, active) + panels;
  _cfgShown = true;
  if (_cfgRoutingAvail) loadRoutingEntries();
  else cfgReapplyPending();
  if (parts.length > 1 && active === parts[0]) cfgFocusItem(parts.join('/'));
}
