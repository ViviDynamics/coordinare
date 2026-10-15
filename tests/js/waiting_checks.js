// 149: checks for the waiting-reason helpers, executed against the shipped
// waiting.js plus the label helpers and esc() it uses.
//
// Usage: node waiting_checks.js <path-to-sliced-js>

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node waiting_checks.js <sliced-js>");
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

// The slice defines no DOM; nothing here registers listeners.
eval(src);

const NOW = Date.parse("2026-10-13T12:00:00Z");
const TWO_HOURS_AGO = "2026-10-13T10:00:00Z";

// --- the queue ----------------------------------------------------------------
const slotsFull = { in_use: 1, limit: 1 };
check("a queued To Do card names the slots",
  waitingReason({ id: "PVTI_65", column: "TODO" },
    { todo_waits: { PVTI_65: { reason: "queued", blockers: [] } }, todo_capacity: slotsFull }),
  { kind: "queued", text: "Queued: 1 of 1 card slots in use", title: waitingReason({ id: "PVTI_65", column: "TODO" },
    { todo_waits: { PVTI_65: { reason: "queued", blockers: [] } }, todo_capacity: slotsFull }).title });
has("the queued title says when it is picked up",
  waitingReason({ id: "x", column: "TODO" }, { todo_waits: { x: { reason: "queued" } }, todo_capacity: slotsFull }).title,
  "picked up when a slot frees");
check("a queued card without a slot count still says what it waits for",
  waitingReason({ id: "x", column: "TODO" }, { todo_waits: { x: { reason: "queued" } } }).text,
  "Queued: waiting for a free card slot");

// --- the session path (session_skip_reasons is the conductor's own record) ----
check("a session held for capacity is queued with the slot count the daemon recorded",
  waitingReason({ id: "PVTI_57", column: "TODO", has_session: true },
    { session_skip_reasons: { PVTI_57: { reason: "pipeline_capacity", blockers: [], capacity: { in_use: 2, limit: 2 } } } }).text,
  "Queued: 2 of 2 card slots in use");
check("a session held by the review cap says how many wait on review",
  waitingReason({ id: "PVTI_58", column: "TODO", has_session: true },
    { session_skip_reasons: { PVTI_58: { reason: "review_capacity", blockers: [], waiting_on_review: 3, review_limit: 3,
      detail: "max_cards_in_review is 3: 3 waiting on review" } } }).text,
  "Queued: 3 cards waiting on review");
check("the review hold keeps the daemon's detail as its title",
  waitingReason({ id: "PVTI_58", column: "TODO", has_session: true },
    { session_skip_reasons: { PVTI_58: { reason: "review_capacity", blockers: [], waiting_on_review: 3, review_limit: 3,
      detail: "max_cards_in_review is 3: 3 waiting on review" } } }).title,
  "max_cards_in_review is 3: 3 waiting on review");
check("a single card waiting on review is singular",
  queuedWaitText("review_capacity", { waiting: 1 }), "Queued: 1 card waiting on review");
check("a To Do card with no recorded wait says nothing, even when the review cap is reached",
  waitingReason({ id: "PVTI_59", column: "TODO" },
    { todo_capacity: { in_use: 0, limit: 2, waiting_on_review: 2, review_limit: 2, hold: null } }).kind,
  "none");
check("a Blocked card with no session keeps the dependency it was moved for",
  waitingReason({ id: "PVTI_60", column: "BLOCKED", has_session: false },
    { todo_waits: { PVTI_60: { reason: "waiting_on", blockers: ["#77"], verdict: "dependency" } } }).text,
  "Waiting on #77");
// 142: a Blocked card is always one kind. With no known reason it is Needs human alone.
check("a Blocked card with a session does not take a To Do wait",
  waitingReason({ id: "PVTI_61", column: "BLOCKED", has_session: true },
    { todo_waits: { PVTI_61: { reason: "waiting_on", blockers: ["#78"] } } }).text,
  "Needs human");
check("queueLimitLabel and the swimlane read the same words",
  queuedWaitText("pipeline_capacity", { capacity: { in_use: 1, limit: 1 } }), "Queued: 1 of 1 card slots in use");
check("an unknown reason has no queued words", queuedWaitText("mystery", {}), null);
check("a session blocked by dependencies names the blockers",
  waitingReason({ id: "PVTI_9", column: "TODO", has_session: true },
    { session_skip_reasons: { PVTI_9: { reason: "dependency_blocked", blockers: [42, 43] } } }).text,
  "Waiting on #42, #43");

// --- waiting on a card ----------------------------------------------------------
check("a To Do card waiting on a blocker names it",
  waitingReason({ id: "PVTI_1", column: "TODO" },
    { todo_waits: { PVTI_1: { reason: "waiting_on", blockers: ["#42"] } } }).text,
  "Waiting on #42");
check("a foreign blocker keeps its repository",
  waitingReason({ id: "PVTI_1", column: "TODO" },
    { todo_waits: { PVTI_1: { reason: "waiting_on", blockers: ["org/other#7"] } } }).text,
  "Waiting on org/other#7");
check("a waiting-on entry with no blockers says nothing rather than guessing",
  waitingReason({ id: "PVTI_1", column: "TODO" },
    { todo_waits: { PVTI_1: { reason: "waiting_on", blockers: [] } } }).kind,
  "none");

// --- needs a human ----------------------------------------------------------------
// 142: the reason is the question itself; the age is kept in the title.
check("a blocked card with an open question shows the question as its reason",
  waitingReason({ id: "PVTI_3", column: "BLOCKED" },
    { open_questions: [{ card_id: "PVTI_3", text: "Which provider?", asked_at: TWO_HOURS_AGO }], now: NOW }).text,
  "Needs human: Which provider?");
has("the title says how long ago the question was asked",
  waitingReason({ id: "PVTI_3", column: "BLOCKED" },
    { open_questions: [{ card_id: "PVTI_3", text: "Which provider?", asked_at: TWO_HOURS_AGO }], now: NOW }).title,
  "Asked 2h ago.");
check("an open question with no text and no timestamp is Needs human alone",
  waitingReason({ id: "PVTI_3", column: "BLOCKED" },
    { open_questions: [{ card_id: "PVTI_3", asked_at: null }], now: NOW }).text,
  "Needs human");
check("a question asked half a minute ago reads in seconds in the title",
  waitingReason({ id: "PVTI_3", column: "BLOCKED" },
    { open_questions: [{ card_id: "PVTI_3", text: "Which provider?", asked_at: "2026-10-13T11:59:30Z" }], now: NOW }).title,
  "Asked 30s ago. Which provider?");
check("an open question for another card does not leak across",
  waitingReason({ id: "PVTI_4", column: "BLOCKED" },
    { open_questions: [{ card_id: "PVTI_3", text: "Which provider?", asked_at: TWO_HOURS_AGO }], now: NOW }).text,
  "Needs human");
check("an escalated To Do card needs a human",
  waitingReason({ id: "PVTI_5", column: "TODO" },
    { todo_waits: { PVTI_5: { reason: "escalated", blockers: [] } } }).text,
  "Needs human: the advocate escalated it to a person");

// --- filtered -----------------------------------------------------------------
check("a card assigned to someone else is filtered out",
  waitingReason({ id: "PVTI_6", column: "TODO" },
    { todo_waits: { PVTI_6: { reason: "assigned_elsewhere", blockers: [] } } }).text,
  "Filtered out: assigned to someone else");
check("an unassigned card the filter leaves out says so",
  waitingReason({ id: "PVTI_7", column: "TODO" },
    { todo_waits: { PVTI_7: { reason: "unassigned", blockers: [] } } }).text,
  "Filtered out: unassigned");

// --- nothing to say -----------------------------------------------------------
check("an unknown wait reason renders nothing",
  waitingReason({ id: "PVTI_8", column: "TODO" }, { todo_waits: { PVTI_8: { reason: "mystery" } } }).kind,
  "none");
check("a card with no entry renders nothing", waitingReason({ id: "PVTI_8", column: "TODO" }, {}).kind, "none");
check("an in-progress card is progressing, so nothing is said",
  waitingReason({ id: "PVTI_9", column: "IN_PROGRESS" }, { todo_waits: { PVTI_9: { reason: "queued" } } }).kind,
  "none");
check("a missing card id renders nothing", waitingReason({ column: "TODO" }, {}).kind, "none");
check("a missing snapshot renders nothing", waitingReason({ id: "x", column: "TODO" }, undefined).kind, "none");
check("a prototype key is not a wait", waitingReason({ id: "constructor", column: "TODO" }, {}).kind, "none");

// --- the To Do header -----------------------------------------------------------
check("the header summarises the slots and the queue",
  todoHeaderTitle({ in_use: 1, limit: 1 }, 3), "1 of 1 card slots in use · 3 queued");
check("the header without a queue leaves the queue out",
  todoHeaderTitle({ in_use: 0, limit: 2 }, 0), "0 of 2 card slots in use");
check("no capacity means no header tooltip", todoHeaderTitle(null, 3), "");
check("with a review cap the header says how many wait on review against it",
  todoHeaderTitle({ in_use: 1, limit: 1, waiting_on_review: 2, review_limit: 3 }, 0),
  "1 of 1 card slots in use · 2 of 3 cards waiting on review");
check("without a review cap the header leaves it out",
  todoHeaderTitle({ in_use: 1, limit: 1, waiting_on_review: 2, review_limit: null }, 1),
  "1 of 1 card slots in use · 1 queued");

// --- the rendered line ----------------------------------------------------------
check("a none reason renders no markup", waitingLineHtml({ kind: "none", text: "", title: "" }), "");
check("a missing reason renders no markup", waitingLineHtml(undefined), "");
const html = waitingLineHtml({ kind: "waiting_on", text: "Waiting on #42", title: "t" });
has("the line carries its kind", html, 'data-wait="waiting_on"');
has("the line holds its text", html, ">Waiting on #42</div>");
const hostile = waitingLineHtml({
  kind: "waiting_on",
  text: 'Waiting on <img src=x onerror="alert(1)">',
  title: '"><script>x</script>',
});
check("the text from GitHub is escaped", hostile.indexOf("<img") === -1 && hostile.indexOf("&lt;img") !== -1, true);
check("the title is escaped", hostile.indexOf("<script>") === -1, true);

// --- the glossary is the only source of the words -----------------------------
check("the kind label comes from the glossary", waitLabel("needs_human"), "Needs human");
check("the glossary covers every kind the function names",
  ["queued", "waiting_on", "needs_human", "filtered"].map(waitLabel), ["Queued", "Waiting on", "Needs human", "Filtered out"]);

console.log(`SUMMARY ${passed} ${failed}`);
if (failed > 0) process.exit(1);
