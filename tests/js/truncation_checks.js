// 135: runs the truncation line's functions, shipped verbatim, under node. Each check
// names the wording or the absence it pins. Driven by tests/unit/test_135_truncation_line.py,
// which slices the source out of the shipped scripts; no DOM, no browser.
//
// Usage: node truncation_checks.js <extracted-source.js>
// Exits non-zero on any failed check. Prints "SUMMARY <passed> <failed>" last.
//
// Deliberately NOT strict mode: the shipped block is loaded with a direct eval, and only
// in sloppy mode do its function declarations land in this module's scope.
const fs = require('fs');

const sourcePath = process.argv[2];
if (!sourcePath) {
  console.error('usage: node truncation_checks.js <extracted-source.js>');
  process.exit(2);
}
eval(fs.readFileSync(sourcePath, 'utf8'));

let passed = 0;
let failed = 0;
function check(name, ok, detail) {
  if (ok) { passed++; return; }
  failed++;
  console.log('FAIL ' + name + (detail ? ' :: ' + detail : ''));
}

const SESSION = 'sess-135';
const WHEN = '2026-10-13T09:00:00+00:00';
const LINK_ORIGIN = '/runs/' + SESSION + '?at=';

function delta(seq, text, extra) {
  return Object.assign({
    seq: seq, activity_type: 'progress', text: text, summary: '', status: '',
    truncated: false, text_length: text.length, summary_length: 0, hidden_updates: null,
    log_dropped: null, card_id: 'C1', stage: 'implementing', session_id: SESSION,
    performer_id: 'P1', is_delta: true, stream_id: 'st1', event_at: WHEN,
  }, extra || {});
}

function marker(seq, hidden) {
  return delta(seq, 'stream st1 exceeded 200 updates', {
    activity_type: 'stream_truncated', is_delta: false, hidden_updates: hidden,
  });
}

function clipped(seq, length, extra) {
  var kept = 'x'.repeat(200);
  return delta(seq, kept, Object.assign({
    truncated: true, text_length: length, is_delta: false, activity_type: 'progress',
  }, extra || {}));
}

// --- the wording ----------------------------------------------------------

var one = afCutHtml([clipped(1, 1240)]);
check('clipped text names its kept and hidden length',
  one.indexOf('text clipped at 200 characters (1,040 characters hidden)') !== -1, one);
check('clipped text links to the full transcript at the event time, named by its short run id',
  one.indexOf(LINK_ORIGIN) !== -1 && one.indexOf('Full transcript of <a') !== -1 &&
  one.indexOf('>run sess-1</a>') !== -1 && one.indexOf('title="' + SESSION + '"') !== -1, one);

var over = [];
for (var i = 0; i < 200; i++) over.push(delta(100 + i, 'w' + i + ' '));
over.push(marker(400, 112));
var cap = afCutHtml(over);
check('a group over the update cap says Showing N of M',
  cap.indexOf('Showing 200 of 312 updates') !== -1, cap);
check('the cap line links to the transcript too', cap.indexOf(LINK_ORIGIN) !== -1, cap);

var summaryCut = afCutHtml([delta(7, 'Bash', {
  activity_type: 'tool_use', summary: 's'.repeat(160), summary_length: 300, text: 'Bash',
  is_delta: false, stream_id: '' })]);
check('a clipped tool summary names its own limit',
  summaryCut.indexOf('tool summary clipped at 160 characters (140 characters hidden)') !== -1, summaryCut);

var both = afCutHtml([clipped(20, 1000), clipped(21, 1500), marker(22, 5)]);
check('several clipped updates are counted together',
  both.indexOf('text clipped at 200 characters in 2 updates (2,100 characters hidden)') !== -1, both);
check('a stream cap and a text clip read as one line',
  both.indexOf('Showing 2 of 7 updates') !== -1 && both.indexOf('text clipped') !== -1, both);

// --- nothing is shown when nothing was cut --------------------------------

check('an uncut group shows no line', afCutHtml([delta(30, 'hello')]) === '',
  afCutHtml([delta(30, 'hello')]));
check('a marker with no hidden updates shows no count line',
  afCutHtml([marker(31, 0)]).indexOf('Showing') === -1);
check('an entry from before 135 shows nothing even when truncated',
  afCutHtml([delta(32, 'x'.repeat(200), { truncated: true, text_length: null })]) === '');
check('an old marker with no count shows nothing',
  afCutHtml([marker(33, null)]) === '');
check('an empty group shows nothing', afCutHtml([]) === '');

// --- a run link only where there is a run ---------------------------------

var orphan = afCutHtml([clipped(40, 900, { session_id: '', is_delta: false })]);
check('a clipped lifecycle row with no run still says what was cut',
  orphan.indexOf('text clipped at 200 characters (700 characters hidden)') !== -1, orphan);
check('a clipped row with no run gets no link', orphan.indexOf('href') === -1, orphan);

var hostileSession = afCutHtml([clipped(41, 900, { session_id: '<img src=x onerror=1>' })]);
check('a hostile session id never reaches the link',
  hostileSession.indexOf('<img') === -1 && hostileSession.indexOf('href') === -1, hostileSession);

// --- numbers and the dropped-entries line ---------------------------------

check('afCount groups thousands', afCount(1234567) === '1,234,567', afCount(1234567));
check('afCount leaves short numbers alone', afCount(999) === '999', afCount(999));
check('afDroppedText is empty when nothing was dropped', afDroppedText(0, 2000, false) === '');
check('afDroppedText is empty when the count is missing', afDroppedText(null, 2000, false) === '');
check('afDroppedText names the dropped count and the capacity the server sent',
  afDroppedText(312, 500, false).indexOf('312 entries were dropped to keep the newest 500') !== -1,
  afDroppedText(312, 500, false));
check('afDroppedText says it covers every symphony when one is selected',
  afDroppedText(312, 500, true).indexOf('across all symphonies') !== -1, afDroppedText(312, 500, true));
check('afDroppedText does not claim a scope when everything is shown',
  afDroppedText(312, 500, false).indexOf('across') === -1, afDroppedText(312, 500, false));

// --- code points, not UTF-16 units (review 135, item 3) -------------------

var EMOJI = '😀';   // one code point, two UTF-16 units
var emojiClipped = afCutHtml([clipped(50, 210, { text: EMOJI.repeat(200) })]);
check('a clipped emoji text counts code points, so its line appears',
  emojiClipped.indexOf('text clipped at 200 characters (10 characters hidden)') !== -1, emojiClipped);
var mixedClipped = afCutHtml([clipped(51, 300, { text: ('a' + EMOJI + 'b').repeat(66) + 'a' + EMOJI })]);
check('mixed astral text counts code points too',
  mixedClipped.indexOf('text clipped at 200 characters (100 characters hidden)') !== -1, mixedClipped);
check('afCodePoints counts an emoji as one', afCodePoints(EMOJI + 'x') === 2, String(afCodePoints(EMOJI + 'x')));

// --- a marker with nothing shown never reads as "Showing 0" (review item 4) ----

var orphanCut = afCutHtml([marker(60, 5)]);
check('a marker with no shown updates says how many are not shown',
  orphanCut.indexOf('5 updates not shown') !== -1, orphanCut);
check('a marker with no shown updates never claims a shown count of 0',
  orphanCut.indexOf('Showing 0') === -1, orphanCut);
check('the group summary counts a marker-only group as not shown',
  afCountText([marker(61, 1)]) === '(1 update not shown)', afCountText([marker(61, 1)]));
check('a group with shown updates keeps the plain summary count',
  afCountText([delta(62, 'a'), delta(63, 'b')]) === '(2 updates)', afCountText([delta(62, 'a'), delta(63, 'b')]));

console.log('SUMMARY ' + passed + ' ' + failed);
process.exit(failed === 0 ? 0 : 1);
