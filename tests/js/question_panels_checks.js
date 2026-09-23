// 355: executes the dashboard's inlined Open Questions + Clarification History
// JavaScript against a minimal DOM stub, so the card-attribution rendering has
// a real gate (mirrors the activity-feed harness in activity_feed_checks.js).
//
// Driven by tests/unit/test_355_client_js.py, which slices the JS verbatim out
// of _DASHBOARD_HTML and passes the path here. No browser, no npm install.
//
// Usage: node question_panels_checks.js <extracted-panels.js>
// Exits non-zero on any failed check. Prints "SUMMARY <passed> <failed>" last.
//
// Deliberately NOT strict mode: the shipped block is loaded with a direct eval,
// and only in sloppy mode do its declarations land in this module's scope.
const fs = require('fs');

const panelsJsPath = process.argv[2];
if (!panelsJsPath) {
  console.error('usage: node question_panels_checks.js <extracted-panels.js>');
  process.exit(2);
}

// DOM stub — models exactly the operations the panel JS performs: getElementById
// returning elements with a style object and an innerHTML property.
const els = {};
function el(id) {
  if (!els[id]) els[id] = { id: id, style: {}, innerHTML: '' };
  return els[id];
}
const document = { getElementById: (id) => el(id) };

let passed = 0;
let failed = 0;
function check(name, cond) {
  if (cond) { passed++; }
  else { failed++; console.error('FAIL: ' + name); }
}

eval(fs.readFileSync(panelsJsPath, 'utf8'));

// 1. Attributed entries render card number (linked), title, stage, text, age.
el('questions-card').innerHTML = '';
renderQuestionPanels({
  open_questions: [{
    card_id: 'card-abc',
    card_number: 106,
    card_title: 'Weekly timesheet submission',
    stage: 'implementing',
    text: 'Which calendar backend?',
    asked_at: new Date(Date.now() - 120000).toISOString(),
    issue_url: 'https://github.com/org/repo/issues/106',
  }],
  issue_url: null,
});
let q = el('questions-list').innerHTML;
check('attributed: card number rendered', q.includes('#106'));
check('attributed: issue link href', q.includes('href="https://github.com/org/repo/issues/106"'));
check('attributed: card title', q.includes('Weekly timesheet submission'));
check('attributed: stage', q.includes('(implementing)'));
check('attributed: question text', q.includes('Which calendar backend?'));
check('attributed: asked-at age', q.includes('asked ') && q.includes(' ago'));
check('attributed: panel visible', el('questions-card').style.display === '');

// 2. Legacy bare strings (old payloads) render without error, with the
//    panel-level issue link preserved.
el('questions-list').innerHTML = '';
renderQuestionPanels({
  open_questions: ['Legacy free-floating question'],
  issue_url: 'https://github.com/org/repo/issues/106',
});
q = el('questions-list').innerHTML;
check('legacy: text rendered', q.includes('Legacy free-floating question'));
check('legacy: view-issue link kept', q.includes('View issue'));

// 3. Legacy bare strings with no issue_url anywhere: still renders.
el('questions-list').innerHTML = '';
renderQuestionPanels({ open_questions: ['Legacy question, no link'], issue_url: null });
check('legacy no link: text rendered', el('questions-list').innerHTML.includes('Legacy question, no link'));

// 4. Empty list hides the panel.
renderQuestionPanels({ open_questions: [] });
check('empty: panel hidden', el('questions-card').style.display === 'none');

// 5. Clarification rounds carry card + stage attribution.
renderQuestionPanels({
  open_questions: [],
  card_clarifications: [{
    questions: ['Which calendar backend?'],
    answer: 'Use the ical backend.',
    card_number: 106,
    card_title: 'Weekly timesheet submission',
    stage: 'implementing',
    issue_url: 'https://github.com/org/repo/issues/106',
  }],
});
q = el('clarifications-list').innerHTML;
check('history: round header', q.includes('Round 1'));
check('history: card number', q.includes('#106'));
check('history: issue link', q.includes('href="https://github.com/org/repo/issues/106"'));
check('history: stage', q.includes('(implementing)'));
check('history: question text', q.includes('Which calendar backend?'));
check('history: answer text', q.includes('Use the ical backend.'));
check('history: panel visible', el('clarifications-card').style.display === '');

// 6. Legacy rounds without attribution still render, unattributed header only.
renderQuestionPanels({
  open_questions: [],
  card_clarifications: [{ questions: ['Old round'], answer: '' }],
});
q = el('clarifications-list').innerHTML;
check('history legacy: round header', q.includes('Round 1'));
check('history legacy: no bogus #undefined', !q.includes('#undefined'));
check('history legacy: question text', q.includes('Old round'));

// 7. Empty history hides the panel.
renderQuestionPanels({ open_questions: [], card_clarifications: [] });
check('history empty: panel hidden', el('clarifications-card').style.display === 'none');

console.log('SUMMARY ' + passed + ' ' + failed);
process.exit(failed > 0 ? 1 : 0);
