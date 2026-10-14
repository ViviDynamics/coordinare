// 154: checks for the Dashboard's status strip and active-run tiles, executed
// against the shipped dashboard scripts (layout.js and what it calls).
//
// Usage: node layout_154_checks.js <path-to-concatenated-js>

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node layout_154_checks.js <concatenated-js>");
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
function checkTrue(name, value) { check(name, !!value, true); }
function checkFalse(name, value) { check(name, !!value, false); }

var _afQuietCards = {};
eval(src);

const NOW = Date.parse("2026-10-13T12:00:30Z");
const RUNNING = { phase: "monitoring_performer", performer_stage: "implementing", card_id: "PVTI_1", card_title: "Fix login", issue_number: 57,
  agent_dispatch_at: "2026-10-13T12:00:00Z", session_id: "run-abc" };

// --- The strip's counts and words ---------------------------------------
check("the strip words for the counts",
  statusCountsText({ active: 1, queued: 0, needsHuman: 2 }), "1 active · 0 queued · 2 needs human");
check("the counts count running tiles, not idle sessions",
  statusCounts({ active_sessions: [RUNNING, { card_id: "PVTI_2", phase: "dispatching", performer_stage: "assessing" }] }).active, 1);
check("a card that needs a person is counted once per question",
  statusCounts({ open_questions: [{ card_id: "PVTI_9", card_number: 9, text: "Which?" }] }).needsHuman, 1);
check("no state counts nothing",
  statusCounts(null), { active: 0, queued: 0, needsHuman: 0 });

// Queued: a To Do card the conductor holds for a free slot.
var queuedState = {
  active_sessions: [],
  symphonies: [{ name: "a", state: { board_snapshot: { TODO: ["PVTI_5", "PVTI_6"] },
    todo_waits: { PVTI_5: { reason: "queued" } } } }],
};
check("a To Do card held for a slot is queued", queuedTodoCount(queuedState), 1);

// --- The utilization summary -------------------------------------------
var eightRoles = [];
for (var i = 0; i < 8; i += 1) eightRoles.push({ role: "r" + i, active: i === 0 ? 1 : 0, max: 1, queued: 0 });
check("eight agents, one busy, none queued",
  utilizationSummaryText({ role_utilization: eightRoles }), "8 agents · 1 busy · 0 queued");
check("one agent is singular",
  utilizationSummaryText({ role_utilization: [{ role: "x", active: 0, max: 2, queued: 3 }] }), "1 agent · 0 busy · 3 queued");
check("no roles, no summary", utilizationSummaryText({ role_utilization: [] }), "");

// The Cards header's counts: the old idle panel's total, and the Done and Backlog counts.
check("the board total is the four columns, with Done and Backlog beside it",
  boardCountsText({ board_summary: { TODO: 5, IN_PROGRESS: 3, IN_REVIEW: 2, DONE: 10, BLOCKED: 1, BACKLOG: 8 } }),
  "Total 20 · Done 10 · Backlog 8");
check("a missing board summary counts nothing", boardCountsText({}), "Total 0 · Done 0 · Backlog 0");

// --- The health dot -------------------------------------------------------
check("a down system makes the dot down",
  healthDotModel({ system_health: { overall: "unavailable", rows: [{ name: "github", status: "down", required: true }] } }).tone,
  "down");
check("healthy rows make the dot healthy",
  healthDotModel({ system_health: { overall: "healthy", rows: [{ name: "github", status: "healthy", required: true }] } }).tone,
  "healthy");
check("without system_health the subsystems decide",
  healthDotModel({ subsystems: [{ name: "github", status: "degraded", required: true }] }).tone, "degraded");

// --- The strip's headline -------------------------------------------------
check("idle headline says only that it is waiting",
  statusStripModel({ phase: "idle", poll_interval_seconds: 30, last_poll_at: "2026-10-13T12:00:18Z" }, null).headline,
  "Idle: waiting for the next check");
check("a running card's headline names the agent and the card",
  statusStripModel({ phase: "monitoring_performer", active_sessions: [RUNNING] }, RUNNING).headline,
  "Watching Implementer on #57");

// --- The active-run tile ------------------------------------------------
var tileState = { agent_dispatch_at: RUNNING.agent_dispatch_at, active_sessions: [RUNNING], open_questions: [] };
var tile = activeRunTileHtml(RUNNING, tileState);
checkTrue("a tile carries its card id", tile.indexOf('data-card-id="PVTI_1"') !== -1);
checkTrue("a tile names its stage", tile.indexOf("Implement") !== -1 || tile.indexOf("Implementer") !== -1);
checkTrue("a tile carries the card's action menu", tile.indexOf('class="card-actions"') !== -1);
checkTrue("a tile carries the run's view link", tile.indexOf("View run") !== -1);
checkTrue("a tile carries the elapsed readout from the dispatch", tile.indexOf('data-elapsed-since="2026-10-13T12:00:00Z"') !== -1);
checkTrue("a tile shows the card's phase once, as the phase pill", (tile.match(/ap-tile-phase/g) || []).length === 1);

var hostile = activeRunTileHtml(Object.assign({}, RUNNING, { card_title: "<img src=x onerror=alert(1)>" }), tileState);
checkFalse("a hostile card title is never written as markup", hostile.indexOf("<img src=x") !== -1);
checkTrue("a hostile card title is escaped", hostile.indexOf("&lt;img src=x") !== -1);

// Tokens: the card's usage when reported, the pending marker while nothing is.
var reported = Object.assign({}, RUNNING, { card_tokens_total: 1234 });
checkTrue("reported tokens read with separators",
  activeRunTileHtml(reported, tileState).indexOf('class="ap-tile-tokens"') !== -1 &&
  activeRunTileHtml(reported, tileState).indexOf(">1,234</strong>") !== -1);
var pendingTile = activeRunTileHtml(Object.assign({}, RUNNING, { card_tokens_total: 0 }), tileState);
checkTrue("a running stage with nothing reported carries the pending note",
  pendingTile.indexOf(USAGE_PENDING_NOTE) !== -1);

// A run past the stale threshold carries the warning the board card used to carry.
var longAgo = Object.assign({}, RUNNING, { agent_dispatch_at: "2020-01-01T00:00:00Z" });
checkTrue("a long run carries the stale warning",
  activeRunTileHtml(longAgo, { agent_dispatch_at: longAgo.agent_dispatch_at }).indexOf("&#9888;") !== -1);
var justStarted = Object.assign({}, RUNNING, { agent_dispatch_at: new Date(Date.now() - 60000).toISOString() });
checkFalse("a fresh run carries no stale warning",
  activeRunTileHtml(justStarted, { agent_dispatch_at: justStarted.agent_dispatch_at }).indexOf("&#9888;") !== -1);

// Quiet marker: observation only.
_afQuietCards.PVTI_1 = true;
checkTrue("a quiet run is marked", activeRunTileHtml(RUNNING, tileState).indexOf("QUIET") !== -1);
delete _afQuietCards.PVTI_1;

// The idle state: the filter hint is the only line besides the title.
var idle = activeRunsIdleHtml({ ownership_hint: "Only cards assigned to coordinare-bot" });
checkTrue("idle says there are no active performers", idle.indexOf("No active performers") !== -1);
checkTrue("idle keeps the filter hint", idle.indexOf("Filter: <strong>Only cards assigned to coordinare-bot</strong>") !== -1);
checkFalse("idle no longer repeats the board counts", idle.indexOf("IN_PROGRESS") !== -1);
checkFalse("idle without a filter has no filter line", activeRunsIdleHtml({ ownership_hint: "" }).indexOf("Filter:") !== -1);

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
