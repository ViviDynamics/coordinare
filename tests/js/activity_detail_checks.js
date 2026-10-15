// 148: checks for the activity feed's detail rendering and kind filters, executed
// against the shipped activity_streams.js plus the shared esc() helper.
//
// Usage: node activity_detail_checks.js <path-to-sliced-js>

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node activity_detail_checks.js <sliced-js>");
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
function truthy(name, condition, detail) {
  check(name, !!condition, true);
  if (!condition && detail) console.error(`  detail: ${detail}`);
}

// A localStorage stand-in. `broken` makes every call throw, as blocked storage does.
function storage(initial, broken) {
  const data = Object.assign({}, initial || {});
  return {
    data,
    getItem(key) {
      if (broken) throw new Error("blocked");
      return Object.prototype.hasOwnProperty.call(data, key) ? data[key] : null;
    },
    setItem(key, value) {
      if (broken) throw new Error("blocked");
      data[key] = String(value);
    },
  };
}

// The page's own helpers that the slice calls but does not define.
let store = storage();
global.window = { localStorage: store };
const buttons = [];
global.document = {
  querySelectorAll(selector) {
    return selector === ".af-chip" ? buttons : [];
  },
};

eval(src);

function afLabel(t) { return String(t || "").toUpperCase(); }
function timeHtml() { return "09:00:00"; }
let renders = 0;
function afRender() { renders += 1; }
function vocabularyKindLabel(kind, value) { return value; }

const KEY = "coordinare.activityChips";

function entry(seq, over) {
  return Object.assign({
    seq: seq,
    timestamp: "2026-10-13T09:00:00+00:00",
    card_id: "PVTI_148",
    card_number: 148,
    card_title: "Activity detail",
    stage: "implementing",
    activity_type: "progress",
    text: "line " + seq,
    summary: "",
    status: "",
    truncated: false,
    session_id: "",
    performer_id: "",
    is_delta: false,
    stream_id: "",
  }, over || {});
}

// --- the chips ---------------------------------------------------------------
check("defaults: Lifecycle, Agent and Errors on, Tools off",
  AF_CHIP_DEFAULTS, { lifecycle: true, agent: true, tools: false, errors: true });
check("with nothing saved the page starts on the defaults", afLoadChips(), AF_CHIP_DEFAULTS);

store = storage({ [KEY]: JSON.stringify({ tools: true, agent: false, lifecycle: "yes", bogus: true }) });
global.window = { localStorage: store };
check("a saved choice is read back, a junk value is ignored",
  afLoadChips(), { lifecycle: true, agent: false, tools: true, errors: true });

store = storage({ [KEY]: "not json" });
global.window = { localStorage: store };
check("corrupt storage falls back to the defaults", afLoadChips(), AF_CHIP_DEFAULTS);

global.window = { localStorage: storage(null, true) };
let blocked = null;
try { blocked = afLoadChips(); } catch (err) { blocked = "threw"; }
check("blocked storage does not break the page", blocked, AF_CHIP_DEFAULTS);
global.window = { localStorage: store = storage() };
_afChips = afLoadChips();

// --- kinds and visibility ----------------------------------------------------
check("a tool call is a tools row", afRowKind(entry(1, { activity_type: "tool_use" })), "tools");
check("a tool result is a tools row", afRowKind(entry(1, { activity_type: "tool_result" })), "tools");
check("narration is an agent row", afRowKind(entry(1, { activity_type: "progress" })), "agent");
check("a stage change is a lifecycle row", afRowKind(entry(1, { activity_type: "stage_change" })), "lifecycle");
check("blocked is lifecycle, not an error", afRowKind(entry(1, { activity_type: "blocked" })), "lifecycle");
check("an error event is an errors row", afRowKind(entry(1, { activity_type: "error" })), "errors");

check("tools are hidden by default", afVisible(entry(1, { activity_type: "tool_use" })), false);
check("lifecycle shows without expanding anything", afVisible(entry(1, { activity_type: "stage_change" })), true);
check("narration shows by default", afVisible(entry(1, { activity_type: "progress" })), true);
check("a failed tool result shows under Errors while Tools is off",
  afVisible(entry(1, { activity_type: "tool_result", status: "error" })), true);
check("a successful tool result stays hidden while Tools is off",
  afVisible(entry(1, { activity_type: "tool_result", status: "ok" })), false);

_afChips.tools = true;
check("turning Tools on shows tool calls", afVisible(entry(1, { activity_type: "tool_use" })), true);
_afChips.tools = false;

// --- the toggle --------------------------------------------------------------
const lifecycleChip = { getAttribute: () => "lifecycle", setAttribute() {} };
const toolsChip = { _pressed: "false", getAttribute(name) { return name === "data-chip" ? "tools" : null; },
  setAttribute(name, value) { if (name === "aria-pressed") this._pressed = value; } };
buttons.length = 0;
buttons.push(lifecycleChip, toolsChip);
renders = 0;
onActivityChipToggle({ getAttribute: () => "tools" });
check("a toggle flips its kind", _afChips.tools, true);
check("a toggle redraws the feed once", renders, 1);
check("a toggle is saved for the next visit", JSON.parse(store.data[KEY]).tools, true);
afApplyChipState();
check("the pressed state follows the kind", toolsChip._pressed, "true");
onActivityChipToggle({ getAttribute: () => "not-a-chip" });
check("an unknown chip is ignored and does not redraw", renders, 1);
onActivityChipToggle({ getAttribute: () => "tools" });
check("toggling back restores the default", _afChips.tools, false);

// --- rendering ----------------------------------------------------------------
const tool = entry(10, { activity_type: "tool_use", text: "Bash", summary: "Bash · uv run pytest -q",
  session_id: "s1", performer_id: "implementer" });
const toolHtml = afRowHtml(tool, { key: 10, entries: [tool], truncatedCount: 0 });
truthy("a tool row shows the command it ran", toolHtml.indexOf("Bash · uv run pytest -q") !== -1, toolHtml);
truthy("a tool row is a tools row", toolHtml.indexOf('class="af-row af-row-tools"') !== -1);
truthy("a tool row carries the stable anchor", toolHtml.indexOf('id="af-group-10"') !== -1);
truthy("a tool row has no bare tool-name-only block", toolHtml.indexOf("<pre class=\"af-tool\"><code>Bash</code></pre>") === -1);

const narration = entry(11, { activity_type: "progress", text: "The tests already pass." });
const narrationHtml = afRowHtml(narration, { key: 11, entries: [narration], truncatedCount: 0 });
truthy("narration is an agent row", narrationHtml.indexOf('af-row-agent') !== -1, narrationHtml);
truthy("narration is set as prose, not as a tool block", narrationHtml.indexOf('af-narration') !== -1 &&
  narrationHtml.indexOf('af-tool') === -1);

const stage = entry(12, { activity_type: "stage_change", text: "implementing to reviewing" });
const stageHtml = afRowHtml(stage);
truthy("a lifecycle event is a plain row, not a collapsed group", stageHtml.indexOf("<details") === -1, stageHtml);
truthy("a lifecycle row has its own kind and an icon", stageHtml.indexOf("af-row-lifecycle") !== -1 &&
  stageHtml.indexOf('class="af-icon"') !== -1 && stageHtml.indexOf('aria-hidden="true"') !== -1);
truthy("a lifecycle row keeps its text", stageHtml.indexOf("implementing to reviewing") !== -1);

const failed_result = entry(13, { activity_type: "tool_result", status: "error", text: "exit 2: tests failed" });
const resultHtml = afRawHtml([failed_result]);
truthy("a failed result is marked as an error", resultHtml.indexOf("af-result-error") !== -1, resultHtml);

// --- grouping -----------------------------------------------------------------
const a = entry(20, { activity_type: "tool_use", session_id: "s1", performer_id: "p1" });
const b = entry(21, { activity_type: "tool_use", session_id: "s1", performer_id: "p1" });
const c = entry(22, { activity_type: "progress", session_id: "s1", performer_id: "p1" });
const d = entry(23, { activity_type: "stage_change", session_id: "s1", performer_id: "p1" });
check("two tool calls in a row fold into one group", afCanGroup(a, b), true);
check("narration never folds into a tool group", afCanGroup(a, c), false);
check("a lifecycle event never folds into anything", afCanGroup(a, d) || afCanGroup(d, a), false);

// --- hostile content ----------------------------------------------------------
const hostile = entry(30, { activity_type: "tool_use", text: "Bash",
  summary: "Bash · <img src=x onerror=alert(1)> && rm -rf </pre><script>x()</script>" });
const hostileHtml = afRowHtml(hostile, { key: 30, entries: [hostile], truncatedCount: 0 });
check("a hostile command is escaped, no raw img", hostileHtml.indexOf("<img") === -1, true);
check("a hostile command is escaped, no raw script", hostileHtml.indexOf("<script>") === -1, true);
check("a hostile command still reads as text", hostileHtml.indexOf("&lt;img") !== -1, true);

const hostileLifecycle = entry(31, { activity_type: "blocked", text: "<b onclick=x()>stop</b>" });
const hostileLifecycleHtml = afRowHtml(hostileLifecycle);
check("hostile lifecycle text is escaped", hostileLifecycleHtml.indexOf("<b onclick") === -1, true);

// --- preview -------------------------------------------------------------------
check("the group preview shows the newest tool summary, not its name",
  afPreview([entry(1, { activity_type: "tool_use", text: "Bash", summary: "Bash · ls" })]), "Bash · ls");
check("a successful result is skipped in the preview",
  afPreview([
    entry(1, { activity_type: "tool_use", text: "Read", summary: "Read · a.py" }),
    entry(2, { activity_type: "tool_result", status: "ok", text: "file body" }),
  ]), "Read · a.py");
check("a failed result shows in the preview",
  afPreview([entry(1, { activity_type: "tool_result", status: "error", text: "boom" })]), "boom");

// --- line text ----------------------------------------------------------------
check("a tool call's body line is its summary",
  afLine(entry(1, { activity_type: "tool_use", text: "Bash", summary: "Bash · pwd" })), "Bash · pwd");
check("a tool call with no summary falls back to its text",
  afLine(entry(1, { activity_type: "tool_use", text: "bash: ls" })), "bash: ls");
check("narration body is its text",
  afLine(entry(1, { activity_type: "progress", text: "hello" })), "hello");

// --- source labels (178) ------------------------------------------------------
// Each raw block names the backend event it came from, and carries its transcript record.
const sourced = afRawHtml([
  entry(40, { activity_type: "tool_use", text: "Bash", summary: "Bash · ls", transcript_seq: 7 }),
  entry(41, { activity_type: "tool_result", status: "ok", text: "result: a.py", transcript_seq: 8 }),
  entry(42, { activity_type: "progress", text: "I will check the CI.", transcript_seq: 9 }),
  entry(43, { activity_type: "thinking", text: "Actually, no.", transcript_seq: 10 }),
  entry(44, { activity_type: "output", text: "stray line", transcript_seq: 11 }),
]);
truthy("a tool call is labelled as a tool call", sourced.indexOf('data-source="Tool call"') !== -1, sourced);
truthy("a tool result is labelled as tool output", sourced.indexOf('data-source="Tool output"') !== -1, sourced);
truthy("progress is labelled as assistant text", sourced.indexOf('data-source="Assistant text"') !== -1, sourced);
truthy("a thinking block is labelled as thinking", sourced.indexOf('data-source="Thinking"') !== -1, sourced);
truthy("a raw output line is labelled as an output line", sourced.indexOf('data-source="Output line"') !== -1, sourced);
truthy("the label is an attribute, never text in the body", sourced.indexOf('<span') === -1 &&
  sourced.indexOf('>Assistant text') === -1, sourced);
truthy("a raw output line is set as output, not as narration", sourced.indexOf('<p class="af-output"') !== -1 &&
  sourced.indexOf('stray line') !== -1 && sourced.indexOf('af-narration" data-event-type="output"') === -1, sourced);
truthy("each block carries its event type", sourced.indexOf('data-event-type="thinking"') !== -1 &&
  sourced.indexOf('data-event-type="output"') !== -1, sourced);
truthy("each block carries its transcript record", sourced.indexOf('data-transcript-seq="9"') !== -1 &&
  sourced.indexOf('title="Assistant text, transcript record 9"') !== -1, sourced);
check("a raw output line is an agent row, so the Agent chip shows it", afRowKind(entry(1, { activity_type: "output" })), "agent");
check("a raw output line folds with the burst it arrived in", afStreamType("output"), true);
check("a block with no transcript record carries none", afRawHtml([entry(1, { activity_type: "progress", text: "x" })])
  .indexOf("data-transcript-seq") === -1, true);

// --- one event from each backend, as the feed stores it (178) -----------------
const labelOf = (fields) => {
  const html = afRawHtml([entry(50, fields)]);
  const m = html.match(/data-source="([^"]*)"/);
  return m ? m[1] : null;
};
check("claude: a tool call with a Name · argument summary is a tool call",
  labelOf({ activity_type: "tool_use", text: "Bash", summary: "Bash · ls" }), "Tool call");
check("claude: a tool result whose text starts result: is tool output",
  labelOf({ activity_type: "tool_result", status: "ok", text: "result: a.py" }), "Tool output");
check("codex: streamed command output (a delta tool_use) is tool output",
  labelOf({ activity_type: "tool_use", is_delta: true, stream_id: "item1", text: "PASS test_x" }), "Tool output");
check("codex: command completion (a tool_result whose text is the command) is neutral, not tool output",
  labelOf({ activity_type: "tool_result", status: "ok", text: "npm test" }), "Tool");
check("opencode: a tool input line is neutral, not a tool call",
  labelOf({ activity_type: "tool_use", text: "bash: ls -la", summary: "bash: ls -la" }), "Tool");
check("opencode: a tool output line is neutral, not a tool call",
  labelOf({ activity_type: "tool_use", text: "bash → a.py", summary: "bash → a.py" }), "Tool");
check("progress from any backend is assistant text", labelOf({ activity_type: "progress", text: "hi" }), "Assistant text");
check("thinking is thinking", labelOf({ activity_type: "thinking", text: "hm" }), "Thinking");
check("a raw output line is an output line", labelOf({ activity_type: "output", text: "x" }), "Output line");

// --- hostile values in the title and data attributes (178) --------------------
const hostileKind = afRawHtml([entry(60, { activity_type: '"><img src=x onerror=alert(1)>', text: "body" })]);
check("a hostile kind never opens a tag", hostileKind.indexOf("<img") === -1, true);
check("a hostile kind is labelled neutrally", hostileKind.indexOf('data-source="Event"') !== -1, true);
const protoKind = afRawHtml([entry(61, { activity_type: "constructor", text: "body" })]);
check("a prototype key falls back to Event, not a function", protoKind.indexOf('data-source="Event"') !== -1, true);
const hostileTitle = afRawHtml([entry(62, { activity_type: "progress", text: "x", transcript_seq: 4 })]);
check("a title with no hostile content is escaped and well formed", hostileTitle.indexOf('title="Assistant text, transcript record 4"') !== -1, true);

// --- a merged block names the span of records it joined (178) -----------------
const merged = afRawHtml([
  entry(70, { activity_type: "progress", text: "The ", is_delta: true, stream_id: "s1", transcript_seq: 20 }),
  entry(71, { activity_type: "progress", text: "fix", is_delta: true, stream_id: "s1", transcript_seq: 23 }),
]);
truthy("a merged block links its first record", merged.indexOf('data-transcript-seq="20"') !== -1, merged);
truthy("a merged block marks its last record", merged.indexOf('data-transcript-seq-last="23"') !== -1, merged);
truthy("a merged block says it spans records 20 to 23", merged.indexOf("transcript records 20 to 23") !== -1, merged);
check("a merged block keeps its joined text", merged.indexOf(">The fix<") !== -1, true);

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
