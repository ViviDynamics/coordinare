// 153: checks for the needs-attention derivation, the title composer and the
// banner markup, executed against the shipped attention.js plus esc and fmtAge.
//
// Usage: node attention_checks.js <path-to-sliced-js>

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node attention_checks.js <sliced-js>");
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
    console.error(`FAIL ${name}: expected to find ${JSON.stringify(needle)} in ${JSON.stringify(haystack)}`);
  }
}
function lacks(name, haystack, needle) {
  const ok = String(haystack).indexOf(needle) === -1;
  if (ok) { passed += 1; } else {
    failed += 1;
    console.error(`FAIL ${name}: did not expect ${JSON.stringify(needle)} in ${JSON.stringify(haystack)}`);
  }
}

// attention.js only defines functions and constants at top level; the page
// wiring touches document only when it is called, so it is safe to load here.
eval(src);

const ISSUE = "https://github.com/acme/app/issues/57";
const blockedSession = {
  card_id: "PVTI_1", card_title: "Add login", issue_number: 57, issue_url: ISSUE,
  phase: "blocked", performer_stage: "implementing",
};
const question = {
  card_id: "PVTI_1", card_number: 57, card_title: "Add login", stage: "implementing",
  text: "Should we use OAuth2?", asked_at: "2026-10-13T09:00:00+00:00", issue_url: ISSUE,
};
const downRequired = { name: "github", status: "unavailable", required: true, details: "403 from API", checked_at: "2026-10-13T09:01:00+00:00" };

// --- the derivation -----------------------------------------------------------
check("no state, no items", attentionItems(undefined), []);
check("an empty snapshot has no items", attentionItems({ phase: "idle", active_sessions: [], open_questions: [], subsystems: [] }), []);

const asked = attentionItems({ active_sessions: [blockedSession], open_questions: [question] });
check("a blocked card with a question is one question item, not two",
  asked.map(function(it) { return it.kind; }), ["question"]);
check("the question item carries its card, text and time",
  [asked[0].card_id, asked[0].title, asked[0].detail, asked[0].since],
  ["PVTI_1", "#57 Add login", "Should we use OAuth2?", "2026-10-13T09:00:00+00:00"]);
check("the question links to the issue", asked[0].href, ISSUE);
check("the question's key names the card and the question", asked[0].key, "question:PVTI_1:Should we use OAuth2?");

const blockedOnly = attentionItems({ active_sessions: [blockedSession], open_questions: [] });
check("a blocked card with no question needs a human", blockedOnly.map(function(it) { return it.kind; }), ["needs_human"]);
check("the needs-human item links to the issue", blockedOnly[0].href, ISSUE);
check("the needs-human key names the card", blockedOnly[0].key, "needs_human:PVTI_1");
check("no reason is invented for a block", blockedOnly[0].detail, "");
check("and it has no since", blockedOnly[0].since, null);

// --- links ---------------------------------------------------------------------
const noIssue = attentionItems({ active_sessions: [Object.assign({}, blockedSession, { issue_url: "" })], open_questions: [] });
check("with no issue URL the item links to the run", noIssue[0].href, "/performers/implementing");
const hostileUrl = attentionItems({ active_sessions: [Object.assign({}, blockedSession, { issue_url: "javascript:alert(1)" })], open_questions: [] });
check("a non-web URL is never linked", hostileUrl[0].href, "/performers/implementing");
const hostileStage = attentionItems({ active_sessions: [Object.assign({}, blockedSession, { issue_url: "", performer_stage: "a/b c" })], open_questions: [] });
check("the run address is encoded", hostileStage[0].href, "/performers/a%2Fb%20c");
check("a card with neither URL nor stage links to the dashboard", attentionItems({ active_sessions: [{ card_id: "X", phase: "blocked" }] })[0].href, "/");

check("an http URL is accepted", attentionExternalUrl("http://example.test/x"), "http://example.test/x");
check("a javascript URL is refused", attentionExternalUrl("javascript:alert(1)"), "");
check("a missing URL is refused", attentionExternalUrl(null), "");

// --- legacy and optional shapes ------------------------------------------------
const legacy = attentionItems({ open_questions: ["Should we use API keys?"] });
check("a bare string question is still a question", legacy.map(function(it) { return [it.kind, it.card_id, it.detail]; }),
  [["question", null, "Should we use API keys?"]]);
check("a legacy question is keyed by its text", legacy[0].key, "question:card:Should we use API keys?");

// --- what is not an item -------------------------------------------------------
const notHuman = attentionItems({ active_sessions: [
  { card_id: "A", phase: "monitoring_pr", issue_url: ISSUE, performer_stage: "reviewing" },
  { card_id: "B", phase: "system_error", performer_stage: "implementing" },
  { card_id: "C", phase: "dispatching", performer_stage: "implementing" },
  { card_id: "D", phase: "monitoring_performer", performer_stage: "implementing" },
], open_questions: [] });
check("a PR wait, a retrying error and a live run need no human", notHuman, []);

// --- subsystems ------------------------------------------------------------------
const down = attentionItems({ subsystems: [downRequired] });
check("a required subsystem that is unavailable is an item", down.map(function(it) { return [it.kind, it.key, it.title]; }),
  [["subsystem_down", "subsystem:github", "github"]]);
check("it links to the subsystems table", down[0].href, "#subsystems-section");
check("its since is when it was checked", down[0].since, "2026-10-13T09:01:00+00:00");
check("its detail is the probe detail", down[0].detail, "403 from API");
check("a degraded required subsystem is not down",
  attentionItems({ subsystems: [Object.assign({}, downRequired, { status: "degraded" })] }), []);
check("an optional subsystem that is unavailable is not an item",
  attentionItems({ subsystems: [Object.assign({}, downRequired, { required: false })] }), []);
check("a healthy subsystem is not an item",
  attentionItems({ subsystems: [Object.assign({}, downRequired, { status: "healthy" })] }), []);

// --- order -----------------------------------------------------------------------
const mixed = attentionItems({ active_sessions: [blockedSession, Object.assign({}, blockedSession, { card_id: "PVTI_2", issue_url: "", performer_stage: "" })],
  open_questions: [question], subsystems: [downRequired] });
check("questions, then blocked cards, then subsystems", mixed.map(function(it) { return it.kind; }),
  ["question", "needs_human", "subsystem_down"]);
check("every key is unique", new Set(mixed.map(function(it) { return it.key; })).size, mixed.length);

// --- the title -------------------------------------------------------------------
check("the count goes in front", attentionTitle("Dashboard | Coordinare", 2), "(2) Dashboard | Coordinare");
check("zero adds nothing", attentionTitle("Performers | Coordinare", 0), "Performers | Coordinare");
check("a missing count adds nothing", attentionTitle("Coordinare", undefined), "Coordinare");
check("a count given as text still composes", attentionTitle("Coordinare", "3"), "(3) Coordinare");
check("an empty base is just the count", attentionTitle("", 1), "(1) ");

// --- counts and text -------------------------------------------------------------
check("one item reads singular", attentionCountText(1), "1 needs attention");
check("more read plural", attentionCountText(3), "3 need attention");
check("the Dashboard badge counts everything",
  attentionNavCounts(mixed).dashboard, 3);
check("the Performers badge counts run-linked items only",
  attentionNavCounts(mixed).performers, 1);
check("nothing to count", attentionNavCounts([]), { dashboard: 0, performers: 0 });

// --- the signature ---------------------------------------------------------------
const sigA = attentionSignature(asked);
check("the same set has the same signature", attentionSignature(attentionItems({ active_sessions: [blockedSession], open_questions: [question] })), sigA);
check("order does not change the signature", attentionSignature(mixed), attentionSignature(mixed.slice().reverse()));
check("a new item changes the signature", attentionSignature(mixed) !== attentionSignature(mixed.slice(1)), true);
// Review: a key by position changed when an earlier question was answered.
const second = Object.assign({}, question, { text: "Which port?" });
check("a question keeps its key when one before it is answered",
  attentionItems({ open_questions: [question, second] })[1].key, attentionItems({ open_questions: [second] })[0].key);
// Review: a subsystem is probed again and again while nothing changes.
function downAt(at) {
  return attentionItems({ subsystems: [{ name: "github", required: true, status: "unavailable", checked_at: at }] });
}
check("a later probe of the same down subsystem keeps the signature",
  attentionSignature(downAt("2026-10-13T09:00:00+00:00")), attentionSignature(downAt("2026-10-13T09:05:00+00:00")));
has("the question is set apart from the card title", attentionRowHtml(asked[0]), '</a>: <span class="attention-detail">');
check("a question asked again changes the signature",
  attentionSignature(attentionItems({ open_questions: [Object.assign({}, question, { asked_at: "2026-10-13T10:00:00+00:00" })] })) !== sigA, true);
check("no items, empty signature", attentionSignature([]), "");

// --- the banner markup ----------------------------------------------------------
const hostileTitle = Object.assign({}, question, { card_title: "<img src=x onerror=alert(1)>", text: "<b>why</b>" });
const row = attentionListHtml(attentionItems({ open_questions: [hostileTitle] }));
lacks("a hostile title is not markup", row, "<img");
lacks("a hostile question is not markup", row, "<b>");
has("a hostile title is escaped", row, "&lt;img src=x onerror=alert(1)&gt;");
has("a hostile question is escaped", row, "&lt;b&gt;why&lt;/b&gt;");
has("the kind is a text label", row, '<span class="attention-kind">Question</span>');
has("an external link opens in a new tab", row, 'target="_blank" rel="noopener"');
has("the elapsed time updates on the page timer", row, 'data-elapsed-since="2026-10-13T09:00:00+00:00"');
has("the elapsed time is labelled", row, 'data-elapsed-prefix="asked "');
has("the item carries its key", row, 'data-attention-key="question:PVTI_1:&lt;b&gt;why&lt;/b&gt;"');

const internal = attentionListHtml([{ kind: "needs_human", key: "k", title: "T", detail: "", href: "/performers/implementing", since: null }]);
has("a run link navigates in the page", internal, 'onclick="navigate(event,this.pathname)"');
lacks("a run link opens nothing new", internal, "target=");
const anchor = attentionListHtml([{ kind: "subsystem_down", key: "s", title: "github", detail: "", href: "#subsystems-section", since: null }]);
lacks("an in-page anchor is not routed", anchor, "navigate(");
lacks("an in-page anchor has no elapsed time without a since", anchor, "data-elapsed-since");

// --- the kind labels --------------------------------------------------------------
check("question label", ATTENTION_KIND_LABELS.question, "Question");
check("needs-human label", ATTENTION_KIND_LABELS.needs_human, "Needs human");
check("subsystem label", ATTENTION_KIND_LABELS.subsystem_down, "Subsystem down");

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
