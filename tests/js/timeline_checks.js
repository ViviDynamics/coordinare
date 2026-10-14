// 147: checks for the card pipeline timeline's pure render functions, executed against the
// shipped timeline.js. Covers the status words, durations and run counts, the escaping of
// hostile event text, the tracker's greyed skipped stages, the board dots and the addresses.
//
// Usage: node timeline_checks.js <path-to-sliced-js>

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node timeline_checks.js <sliced-js>");
  process.exit(1);
}
const src = fs.readFileSync(path.resolve(jsPath), "utf8");

let passed = 0;
let failed = 0;
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (ok) {
    passed += 1;
  } else {
    failed += 1;
    console.error(`FAIL ${name}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}
function has(name, haystack, needle) {
  check(name, String(haystack).indexOf(needle) !== -1, true);
}
function lacks(name, haystack, needle) {
  check(name, String(haystack).indexOf(needle) !== -1, false);
}

// The render functions touch no document until a page function runs, so the file evaluates as is.
eval(src);

const SYMPHONY = "alpha-sym";
const CARD = "PVTI_1";
const RUN = "run147a1";

function stage(code, status, extra) {
  return Object.assign({
    stage: code, status: status, run_count: 0, run_count_source: null, duration_seconds: null,
    duration_partial: false, verdict: null, usage: null, usage_reason: "not tracked", runs: [],
  }, extra || {});
}
const STAGE_CODES = ["assessing", "architecting", "implementing", "reviewing", "security", "qa", "documenting", "closing_review"];
function timeline(overrides) {
  const stages = STAGE_CODES.map((code, i) => stage(code, i === 2 ? "current" : (i < 2 ? "done" : "skipped")));
  return Object.assign({
    symphony: SYMPHONY, card_id: CARD, current_stage: "implementing", merged: false, stages: stages,
    events: [], runs_available: true, runs_reason: null, unassigned_runs: 0, not_recorded: [],
  }, overrides || {});
}

// --- status words and icons -------------------------------------------------------
check("every status has a word", timelineStatusText("done"), "Done");
check("a skipped stage says skipped", timelineStatusText("skipped"), "Skipped");
check("an unknown status is shown as it is", timelineStatusText("odd"), "odd");
check("a not recorded status has a question-mark icon", timelineStatusIcon("not_recorded"), "?");

// --- durations and run counts -------------------------------------------------------
check("a short duration is seconds", timelineDuration(42.4), "42s");
check("a minute and a half reads as minutes", timelineDuration(90.5), "1m 31s");
check("an hour reads as hours", timelineDuration(3700), "1h 1m");
check("a missing duration is empty", timelineDuration(null), "");
check("a negative duration is empty", timelineDuration(-4), "");
check("one run is singular", timelineRunsText({ run_count: 1, run_count_source: "transcripts" }), "1 run");
check("two runs are plural", timelineRunsText({ run_count: 2, run_count_source: "transcripts" }), "2 runs");
check("runs counted from events say so", timelineRunsText({ run_count: 1, run_count_source: "lifecycle" }), "1 run (from events)");
check("no run count is not recorded, not zero", timelineRunsText({ run_count: null }), "runs not recorded");

// --- verdicts -------------------------------------------------------------------------
check("no verdict is said", timelineVerdictText(null), "no verdict");
check("a pass names its marker", timelineVerdictText({ result: "pass", marker: "approved" }), "passed (approved)");
check("a failure does not repeat its words", timelineVerdictText({ result: "fail", marker: "changes_requested" }), "changes requested");
check("a qa failure keeps its marker", timelineVerdictText({ result: "fail", marker: "qa_failed" }), "changes requested (qa failed)");

// --- the tracker ------------------------------------------------------------------------
const tracker = timelineTrackerHtml(timeline(), "implementing");
has("the tracker lists every stage", tracker, 'data-stage="closing_review"');
has("a skipped stage is greyed", tracker, 'class="tl-stage tl-skipped"');
has("the picked stage is marked", tracker, "tl-selected");
has("the current stage shows its status", tracker, "In progress");
has("a stage's label comes from the glossary", tracker, "Implement");
const noTokens = timelineTrackerHtml(timeline(), "implementing");
has("an untracked cost says why", noTokens, "not tracked");
has("a stage with no duration says so", noTokens, "duration not recorded");

const cost = timelineTrackerHtml(timeline({
  stages: [stage("implementing", "done", {
    usage: { tracked: true, tokens: { total: 2050, input: 1050, output: 1000, cache_read: 0, cache_write: 0 },
      cost: { usd: 0.13, kind_usd: {}, unknown_reason: null } },
    duration_seconds: 90.5, run_count: 2, run_count_source: "transcripts",
  })],
}), "implementing");
has("the cost is shown from the ledger", cost, "$0.13");
has("the tokens are shown from the ledger", cost, "2,050 tokens");

const unknownCost = timelineTrackerHtml(timeline({
  stages: [stage("reviewing", "done", {
    usage: { tracked: true, tokens: { total: 900 }, cost: { usd: null, unknown_reason: "no price configured" } },
  })],
}), "reviewing");
has("an unpriced cost reads unknown", unknownCost, "unknown");

// --- the stage detail: runs link to their transcripts -----------------------------------
const detail = timelineStageDetailHtml(timeline({
  stages: [stage("implementing", "done", {
    run_count: 1, run_count_source: "transcripts",
    runs: [{ run: RUN, stage: "implementing", started_at: "2026-10-14T09:00:00+00:00", ended_at: "2026-10-14T09:02:30+00:00",
      duration_seconds: 150, outcome: "completed" }],
  })],
}), "implementing");
has("a run links to its transcript", detail, 'href="/runs/alpha-sym/PVTI_1/run147a1"');
has("a run shows its duration", detail, "2m 30s");
has("a run shows its outcome", detail, "completed");
const noRuns = timelineStageDetailHtml(timeline({ runs_available: false, runs_reason: "transcripts are off" }), "implementing");
has("no transcripts says why", noRuns, "transcripts are off");
const emptyRuns = timelineStageDetailHtml(timeline(), "implementing");
has("a stage with no runs says so", emptyRuns, "No runs recorded for this stage.");

// --- the lifecycle list ----------------------------------------------------------------
const events = timelineEventsHtml(timeline({
  events: [
    { at: "2026-10-14T09:00:00+00:00", kind: "dispatched", stage: "implementing", detail: {} },
    { at: "2026-10-14T09:01:00+00:00", kind: "pr_opened", stage: "implementing", detail: { pr_number: 9, pr_url: "https://x" } },
    { at: "2026-10-14T09:02:00+00:00", kind: "verdict", stage: "reviewing", detail: { result: "fail", marker: "changes_requested" } },
    { at: "2026-10-14T09:03:00+00:00", kind: "feedback_relayed", stage: "implementing", detail: { items: 2 } },
    { at: "2026-10-14T09:04:00+00:00", kind: "ci_result", stage: "implementing", detail: { result: "HOLD" } },
    { at: "2026-10-14T09:05:00+00:00", kind: "review_received", stage: "reviewing", detail: { reviewer: "HUMAN", review_state: "CHANGES_REQUESTED" } },
    { at: "2026-10-14T09:06:00+00:00", kind: "override", stage: "implementing", detail: { action: "restart" } },
    { at: "2026-10-14T09:07:00+00:00", kind: "merged", stage: null, detail: {} },
  ],
}));
has("the list names a dispatch", events, "Dispatched");
has("the list names the pull request number", events, "Pull request opened #9");
has("the list names a verdict's outcome", events, "Verdict: changes requested");
has("the list counts relayed items", events, "Feedback relayed (2 items)");
has("the list names the ci result", events, "CI gate: hold");
has("the list names the reviewer", events, "Review received: human changes requested");
has("the list names an override", events, "Override: restart");
has("the list names the merge", events, "Merged");
check("an empty list is said, not guessed", timelineEventsHtml(timeline({ events: [] })).indexOf("No lifecycle events recorded") !== -1, true);

// --- hostile text is escaped everywhere it is shown -------------------------------------
const hostile = "<img src=x onerror=alert(1)>";
const hostileEvents = timelineEventsHtml(timeline({
  events: [{ at: "2026-10-14T09:00:00+00:00", kind: "human_action", stage: null, detail: { action: hostile } }],
}));
lacks("a hostile action is not written as markup", hostileEvents, "<img");
has("a hostile action is written escaped", hostileEvents, "&lt;img");
const hostileNotes = timelineNotesHtml(timeline({ not_recorded: [hostile] }));
lacks("a hostile note is not written as markup", hostileNotes, "<img");
const hostileRun = timelineStageDetailHtml(timeline({
  stages: [stage("implementing", "done", { runs: [{ run: '"><script>x</script>', started_at: null, outcome: null }] })],
}), "implementing");
lacks("a hostile run id opens no script", hostileRun, "<script>");

// --- the board's dots -------------------------------------------------------------------
const dots = timelineDotsHtml({
  current: "implementing",
  statuses: STAGE_CODES.map((code, i) => ({ stage: code, status: i < 2 ? "done" : (i === 2 ? "current" : "skipped") })),
});
check("one dot per stage", (dots.match(/class="tl-dot /g) || []).length, 8);
check("the current stage is the one ringed", (dots.match(/tl-dot-current/g) || []).length, 1);
has("a dot names its stage and status", dots, "Implement: In progress");
check("no strip means no dots", timelineDotsHtml(null), "");
check("a strip with no statuses means no dots", timelineDotsHtml({ statuses: "x" }), "");

// --- finding a card's strip and its symphony -------------------------------------------
const snapshot = {
  symphonies: [{ name: SYMPHONY, state: { board_snapshot: { TODO: [], IN_PROGRESS: [CARD] } } }],
  card_pipeline: { [SYMPHONY]: { [CARD]: { current: "implementing", statuses: [] } } },
};
check("a card's strip is found in any symphony", cardPipelineOf(snapshot, CARD), { current: "implementing", statuses: [] });
check("a card with no strip has none", cardPipelineOf(snapshot, "PVTI_NONE"), null);
check("a card's symphony is the board that holds it", timelineSymphonyFor(snapshot, CARD), SYMPHONY);
check("a lone symphony owns an unknown card", timelineSymphonyFor({ symphonies: [{ name: "solo" }] }, "X"), "solo");

// --- the addresses -----------------------------------------------------------------------
check("the timeline's address encodes the symphony and card",
  timelineUrl("alpha sym", "PVTI_1"), "/api/cards/alpha%20sym/PVTI_1/timeline");
has("a run's transcript address is the viewer's page",
  timelineRunHref(SYMPHONY, CARD, RUN), "/runs/alpha-sym/PVTI_1/run147a1");

// --- the default stage ----------------------------------------------------------------
check("the stage running now is shown first", timelineDefaultStage(timeline({ current_stage: "reviewing" })), "reviewing");
check("with none running, the last stage that ran is shown",
  timelineDefaultStage(timeline({ current_stage: null, stages: [stage("assessing", "done"), stage("implementing", "ran"), stage("qa", "pending")] })),
  "implementing");

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
