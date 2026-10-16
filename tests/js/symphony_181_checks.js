var assert = require('assert');
var exact = '  # Exact\r\n\r\nKeep trailing.  \r\n';
var catalog = [{role: 'implementer', instructions: '# Global'}, {role: 'qa', instructions: 'Global QA'},
  {role: 'advocate', instructions: 'Advocate'}];
var base = {personas: {qa: {instructions: 'QA', scope_behavior: {skim: {max_tool_calls: 3}}}}, overrides: {github_token: '••••••'}};
var body = symRequestBody([{key: 'personas.implementer', after: exact}, {key: 'personas.qa', after: 'QA'}], base);
assert.strictEqual(body.personas.implementer.instructions, exact);
assert.deepStrictEqual(body.personas.qa, base.personas.qa, 'untouched metadata stays intact');
assert.strictEqual(symRequestBody([{key: 'personas.qa', after: ' \n'}], base).personas, null);
assert.strictEqual(symPersonaSummary(catalog, base.personas), '1 of 3 personas overridden: qa');
assert.strictEqual(symPersonaSummary(catalog, {}), '0 of 3 personas overridden');
assert.strictEqual(symPersonaSummary(catalog, {qa: {instructions: '  '}}), '0 of 3 personas overridden');
var html = symPersonasHtml('demo', base.personas, catalog, false);
assert(html.indexOf('1 of 3 personas overridden: qa') >= 0);
assert(html.indexOf('Advocate') >= 0 && html.indexOf('Global QA') >= 0);
assert(html.indexOf('Compare with global') >= 0);
assert(!/textarea|data-md-tool|data-sym-start|data-sym-revert/.test(html), 'read only view has no editor');
html = symPersonasHtml('demo', base.personas, catalog, true);
assert(html.indexOf('data-md-source') >= 0 && html.indexOf('data-sym-start="copy"') >= 0);
assert(html.indexOf('Revert to global default') >= 0 && html.indexOf('Inherits') >= 0 && html.indexOf('Overridden') >= 0);
assert(html.indexOf('replaces') >= 0);
var diff = symPersonaDiff('same\nold\nend', 'same\n<script>\nend');
assert(diff.indexOf('- old') >= 0 && diff.indexOf('+ &lt;script&gt;') >= 0);
assert(!/<script>/.test(diff));
var writes = [], reviews = [], flashes = [], validation = {valid: true}, validationStatus = true;
var CFG_SAVE_TIMEOUT_MS = 30000;
var _symBase = base, _symCfgHash = '"v1"', _cfgPending = null;
var changed = [];
var form = {querySelector: function() { return {}; }, getAttribute: function() { return 'demo'; }};
var button = {closest: function() { return form; }};
function cfgEntries() { return changed; }
function cfgChanged(e) { return e.changed; }
function cfgClearErrors() {}
function cfgFlash(el, text) { flashes.push(text); }
function cfgPendingFor() { return {scope: 'symphony:demo'}; }
function cfgShowReview(el, rows, save) { reviews.push(save); }
function cfgLock() {}
function cfgPut(url, payload) { writes.push({url: url, payload: payload}); }
async function fetch(url, options) {
  writes.push({url: url, body: JSON.parse(options.body)});
  return {ok: validationStatus, status: 400, json: async function() { return validation; }};
}
symSave(button);
assert.strictEqual(reviews.length, 0);
assert.strictEqual(writes.length, 0, 'untouched save does not write');
changed = [{key: 'personas.implementer', label: 'Implementer', after: exact, before: '', changed: true}];
symSave(button);
assert.strictEqual(writes.length, 0, 'review precedes validation and PUT');
(async function() {
  await reviews.pop()();
  assert.strictEqual(writes[0].url, '/api/symphonies/demo/validate');
  assert.strictEqual(writes[1].payload['If-Match'], '"v1"');
  assert.deepStrictEqual(writes[0].body, writes[1].payload.body, 'validate and PUT use the same reviewed snapshot');
  assert.strictEqual(writes[1].payload.body.personas.implementer.instructions, exact);
  writes = []; validation = {valid: false, errors: [{msg: 'too long'}]}; validationStatus = false;
  symSave(button); await reviews.pop()();
  assert.strictEqual(writes.length, 1, 'invalid proposal never PUTs');
  assert(flashes.pop().indexOf('too long') >= 0);
  await configLoaderChecks();
  console.log('symphony 181 checks passed');
})().catch(function(error) { console.error(error); process.exitCode = 1; });

// Exercise the real Config loader with owned DOM roots and versioned API responses.

async function configLoaderChecks() {
  var activeForm = null, markup = '', version = '"v4"';
  var attributes = {};
  var host = {
    isConnected: true,
    querySelector: function() { return activeForm; },
    setAttribute: function(k, v) { attributes[k] = v; },
    getAttribute: function(k) { return attributes[k]; },
  };
  Object.defineProperty(host, 'innerHTML', {
    get: function() { return markup; },
    set: function(html) {
      markup = html;
      activeForm = html.indexOf('sym-config-form') >= 0 ? {
        getAttribute: function() { return 'demo'; },
      } : null;
    },
  });
  document = {querySelector: function(selector) { return selector === '[data-cfg-symphony-host]' ? host : activeForm; }};
  _cfgSymphonyDrafts = {}; _cfgSymphonyName = null;
  cfgRebaseline = function() {};
  cfgSyncOffTargets = function() {};
  cfgRestoreEdits = function() {};
  cfgReapplyPending = function() {};
  cfgOnSettingsPage = function() { return true; };
  symPersonaMount = function() {};
  symPersonaRefresh = function() {};
  symFormHtml = function() { return '<div id="sym-config-form"></div>'; };
  var requested = [];
  fetch = async function(url) {
    requested.push(url);
    return {ok: true, headers: {get: function() { return url === '/api/personas' ? version : '"v4"'; }},
      json: async function() { return url === '/api/personas' ? catalog : {personas: base.personas, overrides: base.overrides}; }};
  };
  changed = [];
  await cfgLoadSymphony('demo', false);
  assert.strictEqual(_symCfgHash, '"v4"');
  assert.deepStrictEqual(requested, ['/api/symphonies/demo', '/api/personas']);
  assert(activeForm, 'matching versions mount the shared form');
  changed = [{key: 'personas.implementer', after: exact, changed: true}];
  cfgRememberSymphony();
  _cfgSymphonyName = 'other';
  assert(cfgSymphonyHasDrafts(), 'offscreen drafts retain leave protection');
  host.isConnected = false;
  _cfgSymReload();
  assert.strictEqual(requested.length, 2, 'an old editor cannot reload over a new page');
  host.isConnected = true;
  version = '"v5"';
  await cfgLoadSymphony('demo', true);
  assert.strictEqual(activeForm, null, 'mixed catalog and symphony versions refuse editing');
  assert(markup.indexOf('Configuration changed while loading') >= 0);
}
