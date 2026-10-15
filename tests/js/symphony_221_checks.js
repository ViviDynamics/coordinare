// #221: the symphony form's pure functions, run as they ship. The Python test concatenates the shipped
// function sources (config.js and app.js) in front of this file, so these are the real functions.
var CFG_SECRET_MASK = '••••••';
var failures = [];
function check(label, ok, detail) { if (!ok) failures.push(label + (detail ? ': ' + detail : '')); }
function same(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

// --- the save body is the reviewed snapshot, built over the symphony the server holds -----------
var base = {
  overrides: {max_concurrent_cards: 2, poll_interval_seconds: 60, project_name: 'kept', github_token: CFG_SECRET_MASK},
  personas: {implementer: {instructions: 'old'}, reviewer: {instructions: 'keep me'}},
};
var entries = [
  {key: 'overrides.max_concurrent_cards', after: 1, secret: false},
  {key: 'overrides.poll_interval_seconds', after: null, secret: false},
  {key: 'overrides.max_cards_in_review', after: null, secret: false},
  {key: 'overrides.max_cards_in_review.nocap', after: true, secret: false},
  {key: 'overrides.github_token', after: '', secret: true},
  {key: 'personas.implementer', after: '  do the work  ', secret: false},
  {key: 'personas.reviewer', after: 'keep me', secret: false},
  {key: 'enabled', after: false, secret: false},
  {key: 'env_spec_files', after: [], secret: false},
];
var body = symRequestBody(entries, base);
check('the shown number replaces its key', body.overrides.max_concurrent_cards === 1, JSON.stringify(body));
check('a cleared number is removed', !('poll_interval_seconds' in body.overrides), JSON.stringify(body));
check('No cap sends an explicit null', body.overrides.max_cards_in_review === null, JSON.stringify(body));
check('an override the form does not show is kept', body.overrides.project_name === 'kept', JSON.stringify(body));
check('an untyped secret keeps its mask', body.overrides.github_token === CFG_SECRET_MASK, JSON.stringify(body));
check('a persona is trimmed', body.personas.implementer.instructions === 'do the work', JSON.stringify(body));
check('an unchanged persona is kept', body.personas.reviewer.instructions === 'keep me', JSON.stringify(body));
check('enabled is sent', body.enabled === false, JSON.stringify(body));
check('an empty spec list sends the default', same(body.env_spec_files, ['README.md']), JSON.stringify(body));

var clearedSecret = symRequestBody([
  {key: 'overrides.github_token', after: '', secret: true, cleared: true},
], {overrides: {github_token: CFG_SECRET_MASK, max_concurrent_cards: 2}, personas: {}});
check('a cleared secret is written absent', !('github_token' in clearedSecret.overrides), JSON.stringify(clearedSecret));
var clearedRows = cfgDiffRows([{label: 'github_token', before: CFG_SECRET_MASK, after: '', secret: true, cleared: true}]);
check('a cleared secret reads (cleared) in the review', clearedRows[0].after === '(cleared)', JSON.stringify(clearedRows));

var typed = symRequestBody([{key: 'overrides.github_token', after: 'ghp_typed000000000000000000000000000', secret: true}], base);
check('a typed secret is what is sent', typed.overrides.github_token === 'ghp_typed000000000000000000000000000', JSON.stringify(typed));

var cleared = symRequestBody([
  {key: 'overrides.max_cards_in_review', after: null, secret: false},
  {key: 'overrides.max_cards_in_review.nocap', after: false, secret: false},
], {overrides: {max_cards_in_review: null}, personas: {}});
check('unticking No cap with an empty number removes the override', cleared.overrides === null, JSON.stringify(cleared));

var emptyPersona = symRequestBody([{key: 'personas.qa', after: '   ', secret: false}], {overrides: {}, personas: {qa: {instructions: 'x'}}});
check('a blank persona inherits the default', emptyPersona.personas === null, JSON.stringify(emptyPersona));

// --- a secret never reaches the review as a value ------------------------------------------------
var rows = cfgDiffRows([{label: 'github_token', before: CFG_SECRET_MASK, after: 'ghp_typed', secret: true}]);
check('a typed secret reads hidden in the review', rows.length === 1 && rows[0].after === '(hidden)', JSON.stringify(rows));
check('the review never shows the typed value', JSON.stringify(rows).indexOf('ghp_typed') === -1, JSON.stringify(rows));

// --- the refusal in plain words ------------------------------------------------------------------
check('404 says the symphony was deleted', /deleted by someone else/.test(cfgSymphonyRefusal(404, {error: "Symphony 'demo' not found"})));
var refused = cfgSymphonyRefusal(422, {error: 'Validation failed: Non-loopback dashboard_host requires dashboard_auth_token'});
check('422 says nothing was saved and gives the reason', refused === 'Not saved: Non-loopback dashboard_host requires dashboard_auth_token. Nothing was changed.', refused);
check('409 without a conflict keeps the server text', cfgSymphonyRefusal(409, {error: 'Cannot delete the last symphony'}) === 'Cannot delete the last symphony');
check('a bodiless failure names its status', cfgSymphonyRefusal(500, {}) === 'Error 500');

// --- the answer to a symphony save ---------------------------------------------------------------
var calls = {flash: [], conflict: [], navigate: [], rebaseline: 0};
function cfgClearErrors() {}
function cfgFlash(el, text, kind) { calls.flash.push([text, kind]); }
function cfgRebaseline() { calls.rebaseline += 1; }
function cfgConflict(el, status, body, reload) { calls.conflict.push(reload); }
function navigate(e, path) { calls.navigate.push(path); }
function cfgHasUnsaved() { return false; }
var _symCfgHash = 'old';
var _cfgPending = {scope: 'symphony:demo'};

var ok = cfgHandleSymphonySave(200, {name: 'demo'}, {}, {}, '"new"');
check('a saved symphony keeps its new version', ok === true && _symCfgHash === '"new"', _symCfgHash);
check('a saved symphony drops its pending edits', _cfgPending === null);
check('a saved symphony is rebaselined, so nothing is unsaved', calls.rebaseline === 1);
check('a saved symphony says Saved', calls.flash[calls.flash.length - 1][0] === 'Saved');

_cfgPending = {scope: 'symphony:demo'};
var stale = cfgHandleSymphonySave(409, {error: 'changed', conflict: true}, {}, {}, null);
check('a stale save is a conflict', stale === false && calls.conflict.length === 1);
check('a stale save keeps the edits for reapply', _cfgPending !== null && _cfgPending.scope === 'symphony:demo');
check('the conflict reloads the symphony form', calls.conflict[0] === cfgReloadSymphony);

_cfgSymReload = function() { calls.reloaded = true; };
calls.conflict[0]();
check('Reload and reapply reloads the symphony form', calls.reloaded === true);

var deleted = cfgHandleSymphonySave(200, {deleted: 'demo'}, {}, {}, null);
check('a deleted symphony leaves for the list', deleted === true && calls.navigate[0] === '/symphonies');

cfgHandleSymphonySave(422, {error: 'Validation failed: bad'}, {}, {}, null);
check('a 422 is shown as Not saved', calls.flash[calls.flash.length - 1][0] === 'Not saved: bad. Nothing was changed.');

if (failures.length) throw new Error(failures.join(' | '));
console.log('SUMMARY ' + 'ok');
