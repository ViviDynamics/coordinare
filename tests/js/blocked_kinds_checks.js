// 142: checks for the Blocked-column kinds, executed against the shipped
// glossary, waiting.js and attention.js plus the esc() and fmtAge() they use.
//
// Usage: node blocked_kinds_checks.js <path-to-sliced-js>

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node blocked_kinds_checks.js <sliced-js>");
  process.exit(1);
}
const src = fs.readFileSync(path.resolve(jsPath), "utf8");

let passed = 0;
let failed = 0;
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (ok) { passed += 1; } else {
    failed += 1;
    console.error(`FAIL ${name}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}
function has(name, haystack, needle) {
  const ok = String(haystack).indexOf(needle) !== -1;
  if (ok) { passed += 1; } else {
    failed += 1;
    console.error(`FAIL ${name}: expected to contain ${JSON.stringify(needle)}, got ${JSON.stringify(haystack)}`);
  }
}
function lacks(name, haystack, needle) {
  const ok = String(haystack).indexOf(needle) === -1;
  if (ok) { passed += 1; } else {
    failed += 1;
    console.error(`FAIL ${name}: expected not to contain ${JSON.stringify(needle)}, got ${JSON.stringify(haystack)}`);
  }
}

// The slice defines no DOM; nothing here registers listeners.
eval(src);

const NOW = Date.parse("2026-10-13T12:00:00Z");
const TWO_HOURS_AGO = "2026-10-13T10:00:00Z";
const ON_BLOCKED = { id: "PVTI_1", column: "BLOCKED", has_session: true };

// --- Needs human: the reason the conductor holds -------------------------------
const asked = waitingReason({ id: "PVTI_1", column: "BLOCKED" },
  { open_questions: [{ card_id: "PVTI_1", text: "Which provider?", asked_at: TWO_HOURS_AGO }], now: NOW });
check("a question is the reason on a Blocked card", asked.text, "Needs human: Which provider?");
check("and the kind is needs_human", asked.kind, "needs_human");
has("the title says when the question was asked", asked.title, "Asked 2h ago.");

const multiline = waitingReason({ id: "PVTI_1", column: "BLOCKED" },
  { open_questions: [{ card_id: "PVTI_1", text: "Line one\n\n  Line two" }], now: NOW });
check("a multi-line reason is one line on the chip", multiline.text, "Needs human: Line one Line two");

const long = "x".repeat(400);
const truncated = waitingReason({ id: "PVTI_1", column: "BLOCKED" },
  { open_questions: [{ card_id: "PVTI_1", text: long }], now: NOW });
check("a long reason is cut to the chip length", truncated.text, "Needs human: " + "x".repeat(157) + "...");
has("and the title keeps the whole reason", truncated.title, long);

// --- Needs human: the cause the conductor named -----------------------------------
const exhausted = waitingReason(ON_BLOCKED, { open_questions: [] });
check("a Blocked card with no question and no cause is Needs human alone",
  exhausted.text, "Needs human");
check("with no cause the kind is still needs_human", exhausted.kind, "needs_human");

const format = waitingReason(Object.assign({}, ON_BLOCKED, { block_cause: "format_exhausted" }), {});
check("a format failure whose retries are spent says so",
  format.text, "Needs human: retries exhausted on a format failure");
check("the title says the conductor stopped the card", format.title,
  "The conductor stopped this card: retries exhausted on a format failure.");

const closed = waitingReason(Object.assign({}, ON_BLOCKED, { block_cause: "closed_pr" }), {});
check("a closed pull request is named", closed.text, "Needs human: the pull request was closed without merging");

const env = waitingReason(Object.assign({}, ON_BLOCKED, { block_cause: "env_blocked" }), {});
check("an environment block is named", env.text, "Needs human: the environment needs repair");

const unknownCode = waitingReason(Object.assign({}, ON_BLOCKED, { block_cause: "mystery" }), {});
check("an unknown cause is never shown as a reason", unknownCode.text, "Needs human");

const constructorCode = waitingReason(Object.assign({}, ON_BLOCKED, { block_cause: "constructor" }), {});
check("a prototype key is never shown as a reason", constructorCode.text, "Needs human");

// --- Waiting on #N and Queued keep their own kinds ----------------------------------
const dependent = waitingReason({ id: "PVTI_2", column: "BLOCKED", has_session: true },
  { session_skip_reasons: { PVTI_2: { reason: "dependency_blocked", blockers: [77] } } });
check("a dependency wait on a Blocked card is Waiting on #N", dependent.text, "Waiting on #77");
check("and its kind is waiting_on", dependent.kind, "waiting_on");

const queued = waitingReason({ id: "PVTI_3", column: "TODO" },
  { todo_waits: { PVTI_3: { reason: "queued", blockers: [] } }, todo_capacity: { in_use: 1, limit: 1 } });
check("a capacity wait on a To Do card is Queued", queued.text, "Queued: 1 of 1 card slots in use");
check("capacity is never a Blocked-column reason", waitingReason({ id: "PVTI_3", column: "BLOCKED" },
  { todo_waits: { PVTI_3: { reason: "queued", blockers: [] } }, todo_capacity: { in_use: 1, limit: 1 } }).text,
  "Needs human");

// --- the chip markup -----------------------------------------------------------------
const chip = waitingLineHtml(waitingReason(ON_BLOCKED, {}));
has("the chip carries its kind", chip, 'data-wait="needs_human"');
has("the chip shows the words", chip, ">Needs human</div>");

const hostile = waitingReason({ id: "PVTI_4", column: "BLOCKED" },
  { open_questions: [{ card_id: "PVTI_4", text: "<img src=x onerror=alert(1)>Which?" }], now: NOW });
const hostileHtml = waitingLineHtml(hostile);
has("hostile reason text is escaped in the chip", hostileHtml, "&lt;img src=x onerror=alert(1)&gt;Which?");
lacks("and no tag survives into the chip", hostileHtml, "<img");
const hostileTitle = waitingLineHtml(waitingReason({ id: "PVTI_4", column: "BLOCKED" },
  { open_questions: [{ card_id: "PVTI_4", text: 'say "hi" <b>' }], now: NOW }));
has("a quote in the reason cannot close the title attribute", hostileTitle, "&quot;hi&quot;");
lacks("and no raw markup reaches the title", hostileTitle, "<b>");

// --- the session view (tiles and the card detail) -----------------------------------
check("a blocked session names its cause",
  blockedSessionText({ card_id: "PVTI_5", block_cause: "format_exhausted" }, [], {}),
  "Needs human: retries exhausted on a format failure");
check("a blocked session with a question names the question",
  blockedSessionText({ card_id: "PVTI_6" }, [{ card_id: "PVTI_6", text: "Which?" }], {}),
  "Needs human: Which?");
check("a blocked session with a dependency says what it waits on",
  blockedSessionText({ card_id: "PVTI_7" }, [], { PVTI_7: { reason: "dependency_blocked", blockers: [9] } }),
  "Waiting on #9");
check("a blocked session with no known reason is Needs human alone",
  blockedSessionText({ card_id: "PVTI_8" }, [], {}), "Needs human");

// --- the attention banner row ----------------------------------------------------------
const attentionBlocked = attentionItems({ active_sessions: [
  { card_id: "PVTI_9", phase: "blocked", card_title: "Export", issue_number: 9, block_cause: "closed_pr" },
], open_questions: [] });
check("the banner names the kind", attentionBlocked[0].kind, "needs_human");
check("and gives the cause as the reason", attentionBlocked[0].detail, "the pull request was closed without merging");

const attentionNone = attentionItems({ active_sessions: [
  { card_id: "PVTI_10", phase: "blocked", card_title: "Silent", issue_number: 10 },
], open_questions: [] });
check("a stop with no known cause gives the banner no invented reason", attentionNone[0].detail, "");

// --- nothing user-facing says Blocked ---------------------------------------------------
const everyChip = [
  waitingReason(ON_BLOCKED, {}).text,
  waitingReason(Object.assign({}, ON_BLOCKED, { block_cause: "closed_pr" }), {}).text,
  waitingReason(Object.assign({}, ON_BLOCKED, { block_cause: "format_exhausted" }), {}).text,
  waitingReason(Object.assign({}, ON_BLOCKED, { block_cause: "output_truncated" }), {}).text,
  waitingReason(Object.assign({}, ON_BLOCKED, { block_cause: "system_error" }), {}).text,
  waitingReason({ id: "PVTI_2", column: "BLOCKED", has_session: true },
    { session_skip_reasons: { PVTI_2: { reason: "dependency_blocked", blockers: [77] } } }).text,
];
everyChip.forEach(function(text, i) {
  lacks("chip " + i + " does not say blocked", String(text).toLowerCase(), "blocked");
});
lacks("the banner row does not say blocked", attentionListHtml(attentionBlocked).toLowerCase(), "blocked");

// --- 6. the skipped-card list names the wait, never the wire code -------------
check("a dependency skip on the Performers page says Waiting on",
  skipReasonText({ reason: "dependency_blocked", blockers: [42, 43] }), "Waiting on #42, #43");
check("a queued skip keeps the queued words",
  skipReasonText({ reason: "pipeline_capacity", capacity: { in_use: 1, limit: 1 } }), "Queued: 1 of 1 card slots in use");
check("an unrecognised skip is not shown by its wire code",
  skipReasonText({ reason: "blocked_column" }), "Not picked up this check");
check("a skip with no reason is still named", skipReasonText(undefined), "Not picked up this check");

// --- 7. the banner counts Needs human, not a dependency wait -------------------
const depSession = { card_id: "PVTI_70", phase: "blocked", card_title: "Waits", issue_number: 70 };
check("a blocked session waiting on a dependency is not a Needs human item",
  attentionItems({ active_sessions: [depSession], open_questions: [],
    session_skip_reasons: { PVTI_70: { reason: "dependency_blocked", blockers: [9] } } }).length, 0);
check("a dependency wait in a symphony's own state is also not counted",
  attentionItems({ active_sessions: [depSession], open_questions: [],
    symphonies: [{ name: "alpha", state: { session_skip_reasons: { PVTI_70: { reason: "dependency_blocked", blockers: [9] } } } }] }).length, 0);
check("a blocked session with a cause is a Needs human item",
  attentionItems({ active_sessions: [Object.assign({}, depSession, { block_cause: "closed_pr" })], open_questions: [] })[0].kind,
  "needs_human");

console.log("SUMMARY " + passed + " " + failed);
if (failed > 0) { process.exitCode = 1; }
