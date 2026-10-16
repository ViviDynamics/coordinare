const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
vm.runInThisContext(fs.readFileSync(process.argv[2], 'utf8'));

async function checkMergedDetail() {
  const elements = {};
  let detail, stats, rows;
  function element() {
    return {attributes: {}, style: {}, isConnected: true, html: '', listeners: {},
      getAttribute(name) {return this.attributes[name];},
      setAttribute(name, value) {this.attributes[name] = value;},
      removeAttribute(name) {delete this.attributes[name];},
      addEventListener(name, callback) {this.listeners[name] = callback;},
      querySelector() {return null;},
      get innerHTML() {return this.html;},
      set innerHTML(value) {this.html = value;}
    };
  }
  const root = element();
  Object.defineProperty(root, 'innerHTML', {get() {return this.html;}, set(value) {
    this.html = value;
    stats = element();
    detail = element();
    Object.defineProperty(detail, 'innerHTML', {get() {return this.html;}, set(html) {
      this.html = html;
      rows = html.includes('table data-detail') ? Array.from({length: 6}, () => {
        const cell = element();
        return {cell, querySelector: () => cell};
      }) : null;
      for (const match of html.matchAll(/id="([^"]+)"/g)) elements[match[1]] = element();
    }});
    const personaSummary = element();
    detail.querySelector = selector => selector === 'table[data-detail]' && rows
      ? {querySelectorAll: () => rows} : selector === '[data-sym-persona-summary]' ? personaSummary : null;
  }});
  root.querySelector = selector => selector === '[data-symphony-stats]' ? stats : detail;
  elements['symphonies-page-section'] = root;
  global.document = {getElementById: id => elements[id] || null};
  global.location = {pathname: '/symphonies/alpha'};
  global._symShown = null;
  let allowed = true;
  const statsReads = [];
  symphonyStatsScopeAllows = () => allowed;
  loadSymphonyStatsDetail = (name, target, force, revision) => {
    statsReads.push({name, target, force, revision});
    target.innerHTML = allowed ? 'Statistics' : 'Choose All symphonies';
  };
  const calls = [];
  global.fetch = (url, options) => new Promise(resolve => calls.push({url, options, resolve}));
  global.symPersonasHtml = () => '1 of 3 personas overridden: qa';
  global.personaMount = () => {};
  const settle = () => new Promise(resolve => setImmediate(resolve));
  const entry = {name: 'alpha', cycle_count: 3, error_count: 0, last_poll_at: '2026-10-15T12:00:00Z',
    state: {board_snapshot: {TODO: ['card']}, active_card: {title: 'Working'}, last_error: null}};
  renderSymphoniesPage({symphonies: [entry]});
  assert.ok(root.innerHTML.indexOf('data-symphony-stats') < root.innerHTML.indexOf('data-symphony-detail-content'));
  assert.strictEqual(statsReads[0].target, stats);
  assert.strictEqual(statsReads[0].force, true, 'a new page must load even if statistics were cached');
  renderSymphoniesPage({symphonies: [entry]});
  assert.strictEqual(calls.length, 1, 'SSE during the initial read must not duplicate the detail request');
  calls[0].resolve({ok: true, json: async () => ({name: 'alpha', enabled: true, github_project_number: 1,
    state: entry.state, overrides: {max_concurrent_cards: 2}, personas: {qa: {instructions: 'QA'}},
    env_bootstrap_performer_id: 'bootstrap', env_cache: {last_bootstrap_succeeded: false,
      last_bootstrap_error: '<bad image>', cache_dir_ready: false}})});
  await settle();
  assert.strictEqual(calls[1].url, '/api/personas');
  calls[1].resolve({ok: true, json: async () => []});
  await settle();
  const markup = detail.innerHTML;
  ['sym-env-bootstrap-btn', 'sym-wiki-init-btn'].forEach(id => {
    assert.strictEqual(markup.split('id="' + id + '"').length - 1, 1, id + ' occurs once');
    assert.ok(elements[id].listeners.click, id + ' retains its handler');
  });
  assert.ok(markup.includes('Env bootstrap failing for alpha:'));
  assert.ok(markup.includes('&lt;bad image&gt;') && !markup.includes('<bad image>'));
  assert.ok(markup.includes('data-sym-summary-view'));
  assert.ok(markup.includes('/config#symphonies/alpha') && markup.includes('Edit settings'));
  assert.ok(!/textarea|<input|<form/.test(markup), 'settings and personas remain read-only');
  const changed = {...entry, cycle_count: 4, error_count: 1, last_poll_at: '2026-10-15T12:01:00Z',
    state: {...entry.state, active_card: {title: '<new card>'}, last_error: '<new error>'}};
  renderSymphoniesPage({symphonies: [changed]});
  assert.strictEqual(calls.length, 2, 'the SSE fast path does not refetch detail or personas');
  assert.notStrictEqual(statsReads.at(-1).revision, statsReads[0].revision, 'statistics see changed revisions');
  assert.strictEqual(statsReads.at(-1).force, false);
  assert.strictEqual(rows[1].cell.textContent, 4);
  assert.strictEqual(rows[2].cell.textContent, 1);
  assert.strictEqual(rows[3].cell.textContent, '<new card>');
  assert.strictEqual(rows[4].cell.innerHTML, timeHtml(changed.last_poll_at));
  assert.strictEqual(rows[5].cell.textContent, '<new error>');
  assert.strictEqual(detail.innerHTML, markup, 'statistics and SSE preserve settings and action elements');
  assert.strictEqual(stats.innerHTML, 'Statistics');
  for (const [id, endpoint] of [['sym-env-bootstrap-btn', 'env-bootstrap'], ['sym-wiki-init-btn', 'wiki-init']]) {
    const clicked = elements[id].listeners.click();
    const call = calls.at(-1);
    assert.strictEqual(call.url, '/api/symphonies/alpha/' + endpoint);
    assert.strictEqual(call.options.method, 'POST');
    call.resolve({ok: false, json: async () => ({error: 'Try again'})});
    await clicked;
    assert.strictEqual(elements[id].disabled, false);
  }
  allowed = false;
  const before = calls.length;
  renderSymphoniesPage({symphonies: [changed]});
  assert.strictEqual(calls.length, before, 'a scoped viewer cannot fetch another symphony detail');
  assert.strictEqual(root.innerHTML, 'Choose All symphonies');
  console.log('Merged 181/184 detail, actions and SSE checks passed');
}
checkMergedDetail().catch(error => {console.error(error); process.exitCode = 1;});
