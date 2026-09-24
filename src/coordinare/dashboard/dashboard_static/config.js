// 349: 081 config-editing UI (catalog + routing CRUD and the global-config
// page), served verbatim from /static/config.js. Only the config pages call
// into it, so the main dashboard never pays for these bytes.
async function loadGlobalConfigPage() {
  var el = document.getElementById('admin-config-page-section');
  el.innerHTML = '<span class="empty-state">Loading...</span>';
  var res, data;
  try {
    res = await fetch('/api/config/global');
    // 156: the version this page is about to edit. Sent back on save so a
    // colleague's edit landing in between is refused rather than overwritten.
    _adminCfgHash = res.headers.get('ETag');
    data = await res.json();
  } catch(e) {
    el.innerHTML = '<div class="empty-state">Failed to load config</div>';
    return;
  }
  if (!res.ok) {
    el.innerHTML = '<div class="empty-state">' + esc(data.error || 'Error loading config') + '</div>';
    return;
  }
  var numFields = [
    {key:'poll_interval_seconds', label:'Poll interval (seconds)', min:0, max:3600},
    {key:'heartbeat_interval_seconds', label:'Heartbeat interval (seconds)', min:5, max:300},
    {key:'max_concurrent_cards', label:'Max concurrent cards', min:1, max:20},
    {key:'max_feedback_cycles', label:'Max feedback cycles', min:0, max:50},
    {key:'max_closed_pr_attempts_per_issue', label:'Max closed PR attempts per issue', min:0, max:100},
  ];
  var selectFields = [
    {key:'log_level', label:'Log level', options:['debug','info','warning','error']},
    {key:'output_mode', label:'Output mode', options:['human','structured']},
  ];
  var textFields = [
    {key:'assignee_filter', label:'Assignee filter (GitHub login)', placeholder:'(no filter)'},
  ];
  var listFields = [
    {key:'human_reviewers', label:'Human reviewers (comma-separated GitHub logins)'},
    {key:'trusted_bot_reviewers', label:'Trusted bot reviewers (comma-separated)'},
  ];
  var envCacheFields = [
    {key:'env_cache_root', label:'Env cache root (host path)', placeholder:'~/.coordinare/env-caches'},
  ];
  function fieldRow(label, inputHtml) {
    return '<div style="margin-bottom:10px">'
      + '<label style="font-size:11px;color:var(--color-text-muted);display:block;margin-bottom:3px">' + esc(label) + '</label>'
      + inputHtml
      + '</div>';
  }
  var inp_style = 'width:100%;box-sizing:border-box;background:var(--color-bg-base);color:var(--color-text-primary);border:1px solid var(--color-border);border-radius:4px;padding:5px 8px;font-size:12px';
  var numHtml = numFields.map(function(f) {
    return fieldRow(f.label, '<input data-cfg-key="' + esc(f.key) + '" data-cfg-type="number" type="number" min="' + f.min + '" max="' + f.max + '" value="' + esc(String(data[f.key] != null ? data[f.key] : '')) + '" style="' + inp_style + '">');
  }).join('');
  var selHtml = selectFields.map(function(f) {
    var opts = f.options.map(function(o) {
      return '<option value="' + esc(o) + '"' + (String(data[f.key] != null ? data[f.key] : '').toLowerCase() === o ? ' selected' : '') + '>' + esc(o) + '</option>';
    }).join('');
    return fieldRow(f.label, '<select data-cfg-key="' + esc(f.key) + '" data-cfg-type="select" style="' + inp_style + '">' + opts + '</select>');
  }).join('');
  var txtHtml = textFields.map(function(f) {
    var v = data[f.key] != null ? data[f.key] : '';
    return fieldRow(f.label, '<input data-cfg-key="' + esc(f.key) + '" data-cfg-type="text" type="text" value="' + esc(String(v)) + '" placeholder="' + esc(f.placeholder || '') + '" style="' + inp_style + '">');
  }).join('');
  var lstHtml = listFields.map(function(f) {
    var v = Array.isArray(data[f.key]) ? data[f.key].join(', ') : (data[f.key] || '');
    return fieldRow(f.label, '<input data-cfg-key="' + esc(f.key) + '" data-cfg-type="list" type="text" value="' + esc(v) + '" style="' + inp_style + '">');
  }).join('');
  var envCacheHtml = envCacheFields.map(function(f) {
    var v = data[f.key] != null ? data[f.key] : '';
    return fieldRow(f.label, '<input data-cfg-key="' + esc(f.key) + '" data-cfg-type="text" type="text" value="' + esc(String(v)) + '" placeholder="' + esc(f.placeholder || '') + '" style="' + inp_style + '">');
  }).join('');
  el.innerHTML = '<div style="background:var(--color-bg-surface);border:1px solid var(--color-border);border-radius:6px;padding:14px">'
    + '<div style="font-weight:bold;color:var(--color-text-primary);margin-bottom:14px;font-size:13px">Operational</div>'
    + numHtml
    + '<div style="border-top:1px solid var(--color-bg-elevated);margin:14px 0"></div>'
    + '<div style="font-weight:bold;color:var(--color-text-primary);margin-bottom:14px;font-size:13px">Behavior</div>'
    + selHtml + txtHtml
    + '<div style="border-top:1px solid var(--color-bg-elevated);margin:14px 0"></div>'
    + '<div style="font-weight:bold;color:var(--color-text-primary);margin-bottom:14px;font-size:13px">Reviewers</div>'
    + lstHtml
    + '<div style="border-top:1px solid var(--color-bg-elevated);margin:14px 0"></div>'
    + '<div style="font-weight:bold;color:var(--color-text-primary);margin-bottom:14px;font-size:13px">Environment Caching</div>'
    + envCacheHtml
    + '<div style="display:flex;gap:8px;align-items:center;margin-top:8px">'
    + '<button class="action-btn" id="gcfg-save-btn">Save</button>'
    + '<span id="gcfg-save-msg" class="action-msg"></span>'
    + '</div>'
    + '</div>';
  document.getElementById('gcfg-save-btn').addEventListener('click', async function() {
    var msg = document.getElementById('gcfg-save-msg');
    var payload = {};
    el.querySelectorAll('[data-cfg-key]').forEach(function(inp) {
      var k = inp.getAttribute('data-cfg-key');
      var t = inp.getAttribute('data-cfg-type');
      var v = inp.value.trim();
      if (t === 'number') payload[k] = v === '' ? null : Number(v);
      else if (t === 'list') payload[k] = v ? v.split(',').map(function(s){ return s.trim(); }).filter(Boolean) : [];
      else payload[k] = v === '' ? null : v;
    });
    try {
      if (!_adminCfgHash) {
        // Fail closed, like the assistant's Apply: without a version to write
        // against the server will refuse anyway, and saying so here explains what
        // to do instead of surfacing a bare 428.
        msg.textContent = 'Cannot save: this page did not load a configuration version. '
                        + 'Reload the page and try again.';
        msg.style.color = 'var(--color-accent-red)';
        return;
      }
      payload.expected_hash = _adminCfgHash;
      var r = await fetch('/api/config/global', {
        method: 'PUT',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(payload),
      });
      var d = await r.json();
      if (r.ok) {
        msg.textContent = 'Saved — reload triggered';
        msg.style.color = 'var(--color-accent-green)';
        // Carry the new version forward, so a second save without reloading is
        // guarded too. Setting this to null would have made the guard hold exactly
        // once per page load while looking as though it always held.
        _adminCfgHash = d.new_hash || null;
      } else if (r.status === 409) {
        msg.textContent = 'Not saved: the configuration changed since this page loaded. '
                        + 'Reload to see the current values, then make your change again.';
        msg.style.color = 'var(--color-accent-red)';
      } else {
        msg.textContent = versionErrorText(r.status, d);
        msg.style.color = 'var(--color-accent-red)';
      }
    } catch(e) { msg.textContent = 'Network error'; msg.style.color = 'var(--color-accent-red)'; }
    setTimeout(function(){ if(msg) msg.textContent=''; }, 5000);
  });
}

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
var _adminCfgHash = null; // 156: the same, for the older Global Config page (from its ETag)
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
  if (status === 409 && body && body.conflict) {
    return 'Config changed since this page loaded. Reload and re-apply.';
  }
  if (status === 428) return (body && body.error) || 'This save needs a config version.';
  return (body && body.error) || ('Error ' + status);
}
var _cfgVersion = null;
var _cfgRoutingHash = null;     // optimistic-concurrency baseline (routing.yaml, spec-078)
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
    + ' data-orig="' + esc(JSON.stringify(orig)) + '"';
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
    var coerced = cfgCoerce(el);
    if (JSON.stringify(coerced) !== el.getAttribute('data-orig')) {
      changes[el.getAttribute('data-field')] = coerced;
    }
  });
  return changes;
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
function cfgConflict(statusEl) {
  if (!statusEl) return;
  statusEl.innerHTML = '<span style="color:var(--color-accent-orange,#d29922)">'
    + 'Config changed on disk since you loaded it. </span>'
    + '<button type="button" class="cfg-btn" onclick="loadConfigPage()">Reload latest</button>';
}

// Map a save response onto field errors + a status badge. Returns true on success.
// `store` selects which optimistic-concurrency baseline the returned new_hash
// refreshes: 'routing_yaml' → _cfgRoutingHash, otherwise the config.yaml baseline.
function cfgHandleSave(status, body, container, statusEl, store) {
  cfgClearErrors(container);
  if (status === 200 && body.ok) {
    if (body.new_hash) {
      if (store === 'routing_yaml') _cfgRoutingHash = body.new_hash;
      else _cfgHash = body.new_hash;
    }
    if (body.applied === 'hot_reloaded') cfgFlash(statusEl, 'Saved · applied live', 'green');
    else if (body.applied === 'staged_restart') cfgFlash(statusEl, body.message || 'Saved · restart required to apply', 'orange');
    else if (body.applied === 'staged_next_job') cfgFlash(statusEl, 'Saved · applies on next job', 'blue');
    else cfgFlash(statusEl, 'Saved', 'green');
    setTimeout(loadConfigPage, 1000);
    return true;
  }
  if (status === 409) { cfgConflict(statusEl); return false; }
  var errs = body.errors || [];
  cfgApplyErrors(container, errs);
  var general = errs.filter(function(e) { return !e.key; }).map(function(e) { return e.message; }).join('; ');
  cfgFlash(statusEl, general || body.message || ('Error ' + status), 'red');
  return false;
}

async function cfgPut(url, payload, container, statusEl, btn) {
  if (btn) btn.disabled = true;
  try {
    var r = await fetch(url, {method: payload._method || 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload.body)});
    var body = await r.json();
    cfgHandleSave(r.status, body, container, statusEl, payload.store);
  } catch(e) {
    cfgFlash(statusEl, 'Network error', 'red');
  }
  if (btn) btn.disabled = false;
}

function cfgSaveSection(btn, sectionId) {
  var container = btn.closest('[data-section]');
  var statusEl = container.querySelector('[data-status]');
  var changes = cfgCollectChanges(container);
  cfgClearErrors(container);
  if (!Object.keys(changes).length) { cfgFlash(statusEl, 'No changes to save', 'muted'); return; }
  cfgPut('/api/config/section/' + encodeURIComponent(sectionId),
    {body: {store: 'config_yaml', section: sectionId, changes: changes, base_hash: _cfgHash}},
    container, statusEl, btn);
}

function cfgSaveItem(btn, catalog, itemId) {
  var container = btn.closest('[data-item]');
  var statusEl = container.querySelector('[data-status]');
  var changes = cfgCollectChanges(container);
  cfgClearErrors(container);
  if (!Object.keys(changes).length) { cfgFlash(statusEl, 'No changes to save', 'muted'); return; }
  cfgPut('/api/config/catalog/' + encodeURIComponent(catalog) + '/' + encodeURIComponent(itemId),
    {body: {changes: changes, base_hash: _cfgHash}}, container, statusEl, btn);
}

function cfgDeleteItem(btn, catalog, itemId) {
  var container = btn.closest('[data-item]');
  var statusEl = container.querySelector('[data-status]');
  cfgPut('/api/config/catalog/' + encodeURIComponent(catalog) + '/' + encodeURIComponent(itemId),
    {_method: 'DELETE', body: {base_hash: _cfgHash}}, container, statusEl, btn);
}

function cfgToggleAdd(catalog) {
  var form = document.getElementById('cfg-add-' + catalog);
  if (form) form.style.display = (form.style.display === 'none' || !form.style.display) ? '' : 'none';
}

function cfgCreateItem(btn, catalog) {
  var container = btn.closest('[data-item]');
  var statusEl = container.querySelector('[data-status]');
  var item = {};
  container.querySelectorAll('[data-field]').forEach(function(el) {
    var v = cfgCoerce(el);
    if (v !== null && v !== '' && !(Array.isArray(v) && !v.length)) item[el.getAttribute('data-field')] = v;
  });
  cfgClearErrors(container);
  if (!item.name) { cfgFlash(statusEl, 'A name is required', 'red'); return; }
  cfgPut('/api/config/catalog/' + encodeURIComponent(catalog),
    {_method: 'POST', body: {item: item, base_hash: _cfgHash}}, container, statusEl, btn);
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
  return '<div data-rentry style="background:var(--color-bg-base);border:1px solid var(--color-border);border-radius:6px;padding:10px;margin-bottom:10px">'
    + '<div style="font-weight:bold;color:var(--color-text-primary);font-size:13px;margin-bottom:4px">'
    + esc(entry.backend || '') + ' / ' + esc(entry.model || '') + '</div>'
    + cfgRoutingFields(entry)
    + '<div style="display:flex;gap:8px;align-items:center;margin-top:8px">'
    + '<button type="button" class="cfg-btn" onclick="cfgSaveRoutingEntry(this,' + index + ')">Save</button>'
    + '<button type="button" class="cfg-btn cfg-btn-danger" onclick="cfgDeleteRoutingEntry(this,' + index + ')">Delete</button>'
    + '<span data-status style="font-size:11px;margin-left:6px"></span></div></div>';
}
function cfgRoutingAddBlock() {
  return '<div id="cfg-add-routing" data-rentry style="display:none;background:var(--color-bg-base);'
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
  var changes = cfgCollectRoutingChanges(container, false);
  if (!Object.keys(changes).length) { cfgFlash(statusEl, 'No changes to save', 'muted'); return; }
  cfgPut('/api/config/routing/entry/' + index,
    {store: 'routing_yaml', body: {changes: changes, base_hash: _cfgRoutingHash}}, container, statusEl, btn);
}
function cfgDeleteRoutingEntry(btn, index) {
  var container = btn.closest('[data-rentry]');
  var statusEl = container.querySelector('[data-status]');
  cfgPut('/api/config/routing/entry/' + index,
    {store: 'routing_yaml', _method: 'DELETE', body: {base_hash: _cfgRoutingHash}}, container, statusEl, btn);
}
function cfgCreateRoutingEntry(btn) {
  var container = btn.closest('[data-rentry]');
  var statusEl = container.querySelector('[data-status]');
  var entry = cfgCollectRoutingChanges(container, true);
  if (!entry.backend || !entry.model) { cfgFlash(statusEl, 'backend and model are required', 'red'); return; }
  cfgPut('/api/config/routing/entry',
    {store: 'routing_yaml', _method: 'POST', body: {entry: entry, base_hash: _cfgRoutingHash}}, container, statusEl, btn);
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
}

function cfgFieldRow(s) {
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
    // gt/lt are exclusive — render > / < instead of ≥ / ≤ so the displayed range
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
  return '<div data-fieldrow style="padding:8px 0;border-bottom:1px solid var(--color-bg-elevated)">'
    + '<div style="display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap">'
    + '<label style="font-size:12px;color:var(--color-text-primary);font-weight:600">' + esc(s.label || s.key) + '</label>'
    + '<div style="text-align:right">' + meta.join('') + '</div></div>'
    + '<div style="margin-top:3px">' + control + '</div>'
    + help + ch + def
    + '<div data-err style="display:none;font-size:11px;color:var(--color-accent-red);margin-top:3px"></div>'
    + '</div>';
}

// Keep the legacy name as an alias so existing callers/tests resolve.
function cfgSettingRow(s) { return cfgFieldRow(s); }

function cfgItemBlock(item, catalog, editable) {
  var ref = item.referenced_by && item.referenced_by.length
    ? cfgBadge('referenced by: ' + item.referenced_by.join(', '), 'var(--color-accent-blue)')
    : '';
  // A view-only section (editable=false, e.g. personas/symphonies) has no Save
  // action, so it must never offer an edit affordance — force every field
  // read-only, even individually-editable ones, before delegating to cfgFieldRow.
  // Use a shallow copy so the source setting object is never mutated.
  var rows = (item.settings || []).map(function(s) {
    return cfgFieldRow(editable ? s : Object.assign({}, s, {editable: false}));
  }).join('');
  var footer = '';
  if (editable) {
    var iid = esc(item.id);
    var cat = esc(catalog);
    var delTitle = item.deletable ? ''
      : ' title="Protected: remove the references first — ' + esc((item.referenced_by || []).join(', ')) + '"';
    var delBtn = '<button type="button" class="cfg-btn cfg-btn-danger"' + (item.deletable ? '' : ' disabled')
      + delTitle + ' onclick="cfgDeleteItem(this, &quot;' + cat + '&quot;, &quot;' + iid + '&quot;)">Delete</button>';
    footer = '<div style="display:flex;gap:8px;align-items:center;margin-top:8px">'
      + '<button type="button" class="cfg-btn" onclick="cfgSaveItem(this, &quot;' + cat + '&quot;, &quot;' + iid + '&quot;)">Save</button>'
      + delBtn
      + '<span data-status style="font-size:11px;margin-left:6px"></span></div>';
  } else if (!item.deletable) {
    footer = cfgBadge('protected', 'var(--color-text-muted)');
  }
  return '<div data-item style="background:var(--color-bg-base);border:1px solid var(--color-border);border-radius:6px;padding:10px;margin-bottom:10px">'
    + '<div style="font-weight:bold;color:var(--color-text-primary);font-size:13px;margin-bottom:4px">' + esc(item.id) + ref + '</div>'
    + (rows || '<div class="empty-state">No editable fields.</div>')
    + footer
    + '</div>';
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
  return '<div id="cfg-add-' + esc(sec.id) + '" data-item style="display:none;background:var(--color-bg-base);'
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
  var body, footer = '';
  if (sec.kind === 'scalar_group') {
    body = (sec.settings || []).map(cfgFieldRow).join('') || '<div class="empty-state">No settings.</div>';
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
  return '<section data-section="' + esc(sec.id) + '" aria-label="' + esc(sec.title)
    + '" style="background:var(--color-bg-surface);border:1px solid var(--color-border);border-radius:6px;padding:14px;margin-bottom:14px">'
    + '<h3 style="margin:0 0 8px 0;font-size:14px;color:var(--color-text-primary)">' + esc(sec.title) + '</h3>'
    + desc + banner + body + footer
    + '</section>';
}

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
}
