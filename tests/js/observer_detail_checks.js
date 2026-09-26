// 429: checks for the dashboard's session-view observer-verdicts panel,
// executed against the JS the Python test slices out of performers.js.
//
// Usage: node observer_detail_checks.js <extracted-detail.js>
// Exits non-zero on any failed check. Prints "SUMMARY <passed> <failed>" last.
//
// Deliberately NOT strict mode: the shipped block is loaded with a direct eval,
// and only in sloppy mode do its `var`/`function` declarations land in this
// module's scope where the checks below can reach them.
const fs = require('fs');
const path = require('path');

const jsPath = process.argv[2];
if (!jsPath) {
  console.error('usage: node observer_detail_checks.js <extracted-detail.js>');
  process.exit(2);
}

let passed = 0;
let failed = 0;
function check(name, actual, expected) {
  const ok = actual === expected;
  if (ok) { passed += 1; } else {
    failed += 1;
    console.error(`FAIL ${name}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

eval(fs.readFileSync(path.resolve(jsPath), 'utf8'));

const SESSION = {
  phase: 'monitoring_performer',
  performer_stage: 'implementing',
  card_title: 'Ship the thing',
  issue_number: 12,
  agent_dispatch_at: '2026-09-25T00:00:00Z',
  observer_verdicts: [
    { verdict: 'continue', reason: 'steady work', evidence: { tool_uses: 2, quiet_age_s: 310 }, at: '2026-09-25T00:01:00Z' },
    { verdict: 'kill', reason: 'burning without results', evidence: { tool_uses: 0 }, at: '2026-09-25T00:02:00Z' },
  ],
};

const html = renderCardDetailContent(SESSION, {});

check('renders the newest verdict first',
  html.indexOf('burning without results') < html.indexOf('steady work'), true);
check('renders the verdict verb',
  html.includes('kill') && html.includes('continue'), true);
check('renders an evidence summary pair',
  html.includes('tool_uses=0'), true);
check('renders multiple evidence pairs comma-separated',
  html.includes('quiet_age_s=310') && html.includes('tool_uses=2'), true);
check('renders a timestamp for each verdict',
  html.includes('00:02') || html.includes('00:02:00'), true);

const xss = renderCardDetailContent({
  ...SESSION,
  observer_verdicts: [{ verdict: 'continue', reason: '<script>alert(1)</script>', evidence: { e: '"quoted"' }, at: 't' }],
}, {});
check('escapes the reason',
  xss.includes('&lt;script&gt;alert(1)&lt;/script&gt;') && !xss.includes('<script>'), true);
check('escapes evidence values',
  xss.includes('&quot;quoted&quot;') && !xss.includes('"quoted"'), true);

const empty = renderCardDetailContent({ ...SESSION, observer_verdicts: [] }, {});
check('a session with no verdicts renders the placeholder',
  empty.includes('No observer verdicts'), true);

const absent = renderCardDetailContent({ ...SESSION, observer_verdicts: undefined }, {});
check('a session without the field renders the placeholder',
  absent.includes('No observer verdicts'), true);

console.log(`SUMMARY ${passed} ${failed}`);
if (failed > 0) process.exit(1);
