// 138: executes the dashboard's inlined activity-feed JavaScript against a
// minimal DOM stub, so the browser-resident half of the feed has a real gate
// instead of only a manual checklist.
//
// Driven by tests/unit/test_138_client_js.py, which slices the JS verbatim out
// of _DASHBOARD_HTML and passes the path here. No browser, no npm install, no
// timers — node is already required by spec 124 (openwiki), so this adds no
// dependency. Rendering, ARIA announcement behaviour and keyboard focus are
// still out of scope; see the plan's Complexity Tracking.
//
// Usage: node activity_feed_checks.js <extracted-feed.js>
// Exits non-zero on any failed check. Prints "SUMMARY <passed> <failed>" last.
//
// Deliberately NOT strict mode: the shipped block is loaded with a direct eval,
// and only in sloppy mode do its `var`/`function` declarations land in this
// module's scope where the checks below can reach them.
const fs = require('fs');

const feedJsPath = process.argv[2];
if (!feedJsPath) {
  console.error('usage: node activity_feed_checks.js <extracted-feed.js>');
  process.exit(2);
}

// ---------------------------------------------------------------------------
// DOM stub — models exactly the operations the feed JS performs. Children are
// held as HTML strings; rows are self-contained <div class="af-row"> blocks
// with no nesting, so splitting on the opening tag reconstructs them exactly.
// ---------------------------------------------------------------------------
const ROW = '<div class="af-row">';

function splitRows(html) {
  if (!html) return [];
  return html.split(ROW).filter(Boolean).map((s) => ROW + s);
}

class El {
  constructor(id) {
    this.id = id;
    this.rows = [];
    this.style = {};
    this.textContent = '';
    this.value = '';
    this._html = '';
    this._attrs = {};
    this._classes = new Set();
    // Which DOM operation was used is the whole of C1: replacing innerHTML
    // re-creates every row, which is what makes aria-relevant="additions"
    // re-announce rows the user already heard. Comparing the resulting HTML
    // cannot distinguish the two — the strings come out identical — so the
    // operation itself has to be counted.
    this.innerHTMLWrites = 0;
    this.insertCalls = 0;
    this.classList = {
      add: (c) => this._classes.add(c),
      remove: (c) => this._classes.delete(c),
      contains: (c) => this._classes.has(c),
    };
  }
  set innerHTML(v) { this.innerHTMLWrites++; this._html = v; this.rows = splitRows(v); }
  get innerHTML() { return this._html; }
  insertAdjacentHTML(position, html) {
    if (position !== 'afterbegin') throw new Error('unexpected position: ' + position);
    this.insertCalls++;
    this.rows = splitRows(html).concat(this.rows);
    this._html = this.rows.join('');
  }
  get childElementCount() { return this.rows.length; }
  get lastElementChild() { return this.rows.length ? this.rows[this.rows.length - 1] : null; }
  removeChild(child) {
    const i = this.rows.indexOf(child);
    if (i >= 0) this.rows.splice(i, 1);
    this._html = this.rows.join('');
  }
  setAttribute(k, v) { this._attrs[k] = v; }
  getAttribute(k) { return k in this._attrs ? this._attrs[k] : null; }
}

const els = {};
for (const id of ['activity-feed', 'af-empty', 'af-filter', 'af-liveness',
                  'af-stale-note', 'nav-sse-dot', 'disconnected-banner']) {
  els[id] = new El(id);
}
global.document = { getElementById: (id) => (id in els ? els[id] : null) };

// Scaffolding the feed block relies on but does not define — both live
// elsewhere in dashboard.py.
var _lastState = null;
var _renderPerformersCalls = 0;
function renderActivePerformers(s) { _renderPerformersCalls++; }

// Non-strict eval so the shipped `var`/`function` declarations land in this
// module scope and the checks below can reach them.
eval(fs.readFileSync(feedJsPath, 'utf8'));

// ---------------------------------------------------------------------------
let passed = 0;
let failed = 0;
function check(name, condition, detail) {
  if (condition) { console.log('  PASS  ' + name); passed++; }
  else { console.log('  FAIL  ' + name + (detail ? '  -> ' + detail : '')); failed++; }
}
function section(title) { console.log('\n' + title); }

const feed = els['activity-feed'];
const NOW = Date.now();
const iso = (msAgo) => new Date(NOW - msAgo).toISOString();

function entry(seq, over) {
  return Object.assign({
    seq: seq,
    timestamp: iso(0),
    card_id: 'PVTI_demo1',
    card_number: 142,
    card_title: 'Add retry budget',
    stage: 'implementing',
    activity_type: 'progress',
    text: 'line ' + seq,
    truncated: false,
  }, over || {});
}

function resetFeed() {
  _afEntries.length = 0;
  for (const k in _afSeen) delete _afSeen[k];
  for (const k in _afQuietCards) delete _afQuietCards[k];
  _afFilter = '';
  feed.innerHTML = '';
  els['af-filter'].innerHTML = '';
  els['af-filter'].value = '';
  els['af-filter'].setAttribute('data-cards', null);
}

// ---------------------------------------------------------------------------
section('harness — the shipped JS actually loaded');
// `typeof` on an undeclared identifier yields "undefined" rather than throwing,
// so this is safe even if the slice failed to define one of them.
const REQUIRED = {
  esc: typeof esc, afLabel: typeof afLabel, afTime: typeof afTime,
  afRowHtml: typeof afRowHtml, afMatches: typeof afMatches, afAppend: typeof afAppend,
  afRender: typeof afRender, afUpdateEmpty: typeof afUpdateEmpty,
  afUpdateFilterOptions: typeof afUpdateFilterOptions,
  onActivityFilterChange: typeof onActivityFilterChange, afSetLive: typeof afSetLive,
  afNoteMessage: typeof afNoteMessage, afQuietTick: typeof afQuietTick, afTick: typeof afTick,
};
const missing = Object.keys(REQUIRED).filter((fn) => REQUIRED[fn] !== 'function');
check('every feed function is defined', missing.length === 0, 'missing: ' + missing.join(','));
check('silence threshold is ~40 s (FR-026)', AF_SILENCE_MS === 40000, String(AF_SILENCE_MS));
check('client row cap matches the server retention cap', AF_MAX_ROWS === 2000, String(AF_MAX_ROWS));

// ---------------------------------------------------------------------------
section('C11 — empty-state (FR-018)');
afUpdateEmpty();
check('empty state visible on a fresh feed', els['af-empty'].style.display === '');

// ---------------------------------------------------------------------------
section('C1 — live append, no full re-render');
afAppend([entry(0), entry(1)]);
const afterFirstBatch = feed.rows.slice();
check('two rows rendered', feed.childElementCount === 2, String(feed.childElementCount));
check('newest is at the TOP', /line 1/.test(feed.rows[0]) && /line 0/.test(feed.rows[1]));
const writesBefore = feed.innerHTMLWrites;
const insertsBefore = feed.insertCalls;
afAppend([entry(2)]);
check('three rows after the second batch', feed.childElementCount === 3);
check('newest of batch two at top', /line 2/.test(feed.rows[0]));
check('pre-existing rows unchanged',
      feed.rows[1] === afterFirstBatch[0] && feed.rows[2] === afterFirstBatch[1]);
// This is the check that actually holds the line. A live append MUST prepend;
// rewriting innerHTML re-creates every row and makes the live region re-read
// the whole feed, which is the regression C1 exists to catch.
check('a live append PREPENDS and never rewrites innerHTML',
      feed.insertCalls === insertsBefore + 1 && feed.innerHTMLWrites === writesBefore,
      `inserts +${feed.insertCalls - insertsBefore}, innerHTML writes +${feed.innerHTMLWrites - writesBefore}`);
check('empty state hidden once rows exist', els['af-empty'].style.display === 'none');

// ---------------------------------------------------------------------------
section('C2 — seq dedup across reconnect backfill');
const beforeReplay = feed.childElementCount;
afAppend([entry(0), entry(1), entry(2), entry(3)]);
check('only the genuinely new entry was added',
      feed.childElementCount === beforeReplay + 1, String(feed.childElementCount));
const replaySeqs = feed.rows.map((r) => r.match(/line (\d+)/)[1]);
check('no row appears twice', new Set(replaySeqs).size === replaySeqs.length, replaySeqs.join(','));
check('feed is not upside down', replaySeqs[0] === '3' &&
      replaySeqs[replaySeqs.length - 1] === '0');

// ---------------------------------------------------------------------------
section('C3 (timer logic) / C4 — silence timer and not-live rendering');
afNoteMessage();
afTick();
check('live while messages are arriving', _afLive === true);
_afLastMsgAt = Date.now() - 39000;
afTick();
check('still live at 39 s — one dropped keepalive cannot trip it', _afLive === true);
_afLastMsgAt = Date.now() - 41000;
afTick();
check('not live past 40 s', _afLive === false);
check('liveness label flipped', els['af-liveness'].textContent === 'Not live');
check('C4: retained rows are NOT cleared on disconnect (FR-027)', feed.childElementCount === 4);
check('possibly-out-of-date note shown', els['af-stale-note'].style.display === '');
check('reuses the existing nav dot rather than a second indicator',
      els['nav-sse-dot'].classList.contains('disconnected'));
afNoteMessage();
check('C5: returns to live on the next message', _afLive === true);
check('stale note hidden again', els['af-stale-note'].style.display === 'none');

// ---------------------------------------------------------------------------
section('C6 / C7 — quiet marker and clearing');
_lastState = {
  activity_quiet_threshold_seconds: 20,
  active_sessions: [{
    card_id: 'PVTI_demo1', card_title: 'Add retry budget', issue_number: 142,
    performer_stage: 'implementing', agent_dispatch_at: iso(600000),
  }],
};
afQuietTick();
check('not marked while a recent entry exists', !_afQuietCards['PVTI_demo1']);
_afEntries.forEach((e) => { e.timestamp = iso(60000); });
const callsBefore = _renderPerformersCalls;
afQuietTick();
check('marked quiet past the threshold', _afQuietCards['PVTI_demo1'] === true);
const quietRows = feed.rows.filter((r) => /ev-quiet/.test(r));
check('exactly ONE synthetic row for the episode', quietRows.length === 1, String(quietRows.length));
check('synthetic row carries a visible QUIET text prefix, so severity is never ' +
      'colour-only (WCAG 1.4.1)', /">QUIET</.test(quietRows[0]));
check('card marker repainted on the performer tile',
      _renderPerformersCalls === callsBefore + 1);
afQuietTick();
afQuietTick();
check('still exactly one row after repeated ticks',
      feed.rows.filter((r) => /ev-quiet/.test(r)).length === 1);
afAppend([entry(99, { text: 'agent woke up' })]);
check('C7: any real entry clears the marking', !_afQuietCards['PVTI_demo1']);

// ---------------------------------------------------------------------------
section('C8 — quiet after restart, ZERO retained entries (the T034a gate)');
// A session restored after a daemon restart: the feed starts empty (FR-018) and
// a card restored straight into monitoring_performer produces no stage
// transition, so it has no entry to anchor on.
resetFeed();
_lastState = {
  activity_quiet_threshold_seconds: 20,
  active_sessions: [{
    card_id: 'PVTI_restored', card_title: 'Restored card', issue_number: 7,
    performer_stage: 'implementing', agent_dispatch_at: iso(600000),
  }],
};
afQuietTick();
check('restored card marked quiet on agent_dispatch_at ALONE',
      _afQuietCards['PVTI_restored'] === true,
      'the anchor regressed to newest-entry-only — FR-028 exists to prevent exactly this');
check('one synthetic row for it', feed.rows.filter((r) => /ev-quiet/.test(r)).length === 1);

// ---------------------------------------------------------------------------
section('C9 — missing-timestamp safety');
_lastState = {
  activity_quiet_threshold_seconds: 20,
  active_sessions: [{
    card_id: 'PVTI_noanchor', card_title: 'No anchor', issue_number: 8,
    performer_stage: 'implementing',
  }],
};
afQuietTick();
check('a card with neither anchor is left UNMARKED', !_afQuietCards['PVTI_noanchor']);

_lastState = {
  activity_quiet_threshold_seconds: 0,
  active_sessions: [{ card_id: 'PVTI_off', agent_dispatch_at: iso(9999999) }],
};
afQuietTick();
check('threshold 0 disables detection', !_afQuietCards['PVTI_off']);

// ---------------------------------------------------------------------------
section('C10 — filter survives live updates');
resetFeed();
afAppend([
  entry(200, { card_id: 'CARD_A', card_number: 1, card_title: 'Alpha', text: 'a1' }),
  entry(201, { card_id: 'CARD_B', card_number: 2, card_title: 'Beta', text: 'b1' }),
]);
check('filter options built from the cards present in the feed',
      els['af-filter'].innerHTML.includes('#1 Alpha') &&
      els['af-filter'].innerHTML.includes('#2 Beta'));
check('"All cards" option present', els['af-filter'].innerHTML.includes('>All cards<'));
els['af-filter'].value = 'CARD_A';
onActivityFilterChange();
check('filtered to one card', feed.childElementCount === 1 && /a1/.test(feed.rows[0]));
afAppend([entry(202, { card_id: 'CARD_B', card_number: 2, card_title: 'Beta', text: 'b2' })]);
check('a filtered-out card\'s NEW row does not appear',
      feed.childElementCount === 1, String(feed.childElementCount));
afAppend([entry(203, { card_id: 'CARD_A', card_number: 1, card_title: 'Alpha', text: 'a2' })]);
check('the selected card\'s new row does appear', feed.childElementCount === 2);
els['af-filter'].value = '';
onActivityFilterChange();
check('clearing restores the full retained history without a refresh',
      feed.childElementCount === 4, String(feed.childElementCount));
check('restored history is newest-first',
      /a2/.test(feed.rows[0]) && /a1/.test(feed.rows[3]));

// ---------------------------------------------------------------------------
section('escaping — feed text is backend output and reaches innerHTML');
resetFeed();
afAppend([entry(300, {
  card_id: 'CARD_X',
  card_title: '<img src=x onerror=alert(1)>',
  text: '</div><script>alert(1)</script>',
})]);
const rendered = feed.rows[0];
check('no raw <script> in rendered output', !/<script>/.test(rendered), rendered.slice(0, 120));
check('no raw <img in rendered output', !/<img /.test(rendered));

// ---------------------------------------------------------------------------
section('row cap — the client never outgrows the server retention cap');
resetFeed();
const flood = [];
for (let i = 0; i < AF_MAX_ROWS + 250; i++) flood.push(entry(1000 + i));
afAppend(flood);
check('rows bounded at AF_MAX_ROWS', feed.childElementCount === AF_MAX_ROWS,
      String(feed.childElementCount));
check('retained entry list bounded too', _afEntries.length === AF_MAX_ROWS,
      String(_afEntries.length));

console.log('\nSUMMARY ' + passed + ' ' + failed);
process.exit(failed ? 1 : 0);
