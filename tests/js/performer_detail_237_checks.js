// 237 and 239: checks for the performer detail page's renderers, executed against the shipped
// activity_streams.js and performers.js plus the helpers they call.
//
// Usage: node performer_detail_237_checks.js <path-to-sliced-js>

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node performer_detail_237_checks.js <sliced-js>");
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

// The page's own globals the slices touch at load time. Only localStorage is read there.
global.window = { localStorage: { getItem() { return null; }, setItem() {} } };
global.document = { getElementById() { return null; }, querySelectorAll() { return []; } };
global.location = { pathname: "/performers" };

eval(src);

// The Live Events rows: the feed's look, for a run with one id.
const RUN = "run-237-abc";
function ev(over) {
  return Object.assign({
    type: "progress", text: "", detail: "", timestamp: "2026-10-14T09:00:00+00:00",
    is_delta: false, stream_id: "", tool: "", arg: "", status: "", transcript_seq: null,
  }, over);
}

// --- one backend event becomes one feed entry --------------------------------
const call = afEntryFromBackend(ev({ type: "tool_use", tool: "Bash", arg: "uv run pytest -q", text: "Bash" }), RUN);
check("a tool call keeps the tool-call kind", call.activity_type, "tool_use");
check("a tool call's summary is the tool and its argument", call.summary, "Bash · uv run pytest -q");
check("the entry is attributed to the run", call.session_id, RUN);

const bare = afEntryFromBackend(ev({ type: "tool_use", tool: "", arg: "", text: "legacy" }), RUN);
check("a tool call from an older performer falls back to its text", bare.summary, "legacy");

const failed1 = afEntryFromBackend(ev({ type: "tool_use", text: "result: boom", status: "error" }), RUN);
check("a failed tool result is a tool_result", failed1.activity_type, "tool_result");
check("a failed tool result carries its outcome", failed1.status, "error");

const ok1 = afEntryFromBackend(ev({ type: "tool_use", text: "result: fine", status: "ok" }), RUN);
check("a successful tool result carries ok", ok1.status, "ok");

check("narration stays progress", afEntryFromBackend(ev({ type: "progress", text: "hello" }), RUN).activity_type, "progress");
check("thinking keeps its kind", afEntryFromBackend(ev({ type: "thinking", text: "hm" }), RUN).activity_type, "thinking");
check("an unknown event type reads as a raw output line (178)", afEntryFromBackend(ev({ type: "mystery" }), RUN).activity_type, "output");
check("a prototype name reads as a raw output line, not as a method (178)",
  afEntryFromBackend(ev({ type: "constructor" }), RUN).activity_type, "output");
// 178: the performer page labels its rows by source, as the feed does.
check("a performer-page codex delta is labelled tool output (178)",
  /data-source="Tool output"/.test(afEventRowHtml(afEntryFromBackend(ev({ type: "tool_use", is_delta: true, text: "PASS" }), RUN))), true);
check("a performer-page command completion is neutral, not tool output (178)",
  /data-source="Tool"/.test(afEventRowHtml(afEntryFromBackend(ev({ type: "tool_use", status: "ok", text: "npm test" }), RUN))), true);

// --- the rendered Live Events list ----------------------------------------------
const toolHtml = performerEventsHtml([ev({ type: "tool_use", tool: "Bash", arg: "uv run pytest -q", text: "Bash", transcript_seq: 7 })], RUN);
truthy("a tool row shows the command", toolHtml.indexOf("Bash · uv run pytest -q") !== -1, toolHtml);
truthy("a tool row is drawn as a tool call", toolHtml.indexOf('<pre class="af-tool"><code>') !== -1, toolHtml);
truthy("a tool row is labelled from the feed vocabulary", toolHtml.indexOf('>TOOL<') !== -1, toolHtml);
check("a tool row carries no raw wire-type label", toolHtml.indexOf("tool_use:") === -1, true);
truthy("a tool row links to its transcript record", toolHtml.indexOf('href="/runs/' + RUN + '#e7"') !== -1, toolHtml);

const narrationHtml = performerEventsHtml([ev({ type: "progress", text: "Reading the failing test first." })], RUN);
truthy("narration is an Agent row", narrationHtml.indexOf('>AGENT<') !== -1, narrationHtml);
truthy("narration is drawn as narration", narrationHtml.indexOf('af-narration') !== -1, narrationHtml);
check("narration is not drawn as a tool call", narrationHtml.indexOf("af-tool") === -1, true);
check("narration carries no progress: label", narrationHtml.indexOf("progress:") === -1, true);

const errorHtml = performerEventsHtml([ev({ type: "tool_use", text: "result: 1 failed", status: "error" })], RUN);
truthy("a failed result is in the error state", errorHtml.indexOf("af-result-error") !== -1, errorHtml);
truthy("a failed result is labelled RESULT", errorHtml.indexOf(">RESULT<") !== -1, errorHtml);

const markupHtml = performerEventsHtml([ev({ type: "progress", text: "<img src=x onerror=alert(1)>" })], RUN);
check("event text is escaped", markupHtml.indexOf("<img") === -1, true);
truthy("event text is shown escaped", markupHtml.indexOf("&lt;img") !== -1, markupHtml);

const toolArgHtml = performerEventsHtml([ev({ type: "tool_use", tool: "Bash", arg: "echo <b>hi</b>" })], RUN);
check("a tool argument is escaped", toolArgHtml.indexOf("<b>hi</b>") === -1, true);

const empty = performerEventsHtml([], RUN);
truthy("no events says when events appear", empty.indexOf("No events yet") !== -1, empty);
check("no events shows no placeholder or code fallback", /undefined|null|NaN|>-</.test(empty), false);

const many = [];
for (let i = 0; i < 15; i += 1) many.push(ev({ type: "progress", text: "line-" + i }));
const capped = performerEventsHtml(many, RUN);
check("only the newest twelve events show", (capped.match(/af-narration/g) || []).length, 12);
truthy("the oldest shown event is line-3", capped.indexOf("line-3<") !== -1 && capped.indexOf("line-2<") === -1, capped);

const noRun = performerEventsHtml([ev({ type: "progress", text: "x" })], "");
check("an event without a run has no run link", noRun.indexOf("/runs/") === -1, true);

// --- probe lines in the process log ------------------------------------------
const probe = "2026-10-14 09:00:01 [info] claude_code shim request method=HEAD path=/api/hello status=404 latency_ms=0";
const liveRequest = "2026-10-14 09:00:02 [info] claude_code shim request method=POST path=/v1/messages status=200 latency_ms=812";
const probeGet = "2026-10-14 09:00:03 [info] request method=GET path=/api/hello status=404";
const unrelated = "2026-10-14 09:00:04 [info] git checked out HEAD detached";

check("a HEAD probe is a probe", isPerformerProbeLog(probe), true);
check("a GET to the liveness path is a probe", isPerformerProbeLog(probeGet), true);
check("a model request is not a probe", isPerformerProbeLog(liveRequest), false);
check("a line that merely says HEAD is not a probe", isPerformerProbeLog(unrelated), false);

const logsHtml = performerLogsHtml([probe, liveRequest, probe, probeGet]);
truthy("the model request still shows", logsHtml.indexOf("path=/v1/messages") !== -1, logsHtml);
check("no probe line shows", logsHtml.indexOf("/api/hello") === -1, true);

const flood = [];
for (let i = 0; i < 25; i += 1) flood.push(probe);
flood.push("real stderr line");
const after = performerLogsHtml(flood);
truthy("probes do not push real stderr out of the view", after.indexOf("real stderr line") !== -1, after);

const onlyProbes = performerLogsHtml([probe, probe]);
truthy("a log of only probes says so", onlyProbes.indexOf("Only health probes so far") !== -1, onlyProbes);
const noLogs = performerLogsHtml([]);
truthy("no stderr says what appears and when", noLogs.indexOf("appears here as the performer writes it") !== -1, noLogs);
check("no log state shows no placeholder", /undefined|null|NaN|>-</.test(noLogs + onlyProbes), false);

// --- the metrics line ----------------------------------------------------------
const noMetrics = performerMetricsHtml(null, "5");
truthy("no metrics says when they appear", noMetrics.indexOf("appear after the first poll") !== -1, noMetrics);
check("no metrics line has no PID", noMetrics.indexOf("PID") === -1, true);

const firstPoll = performerMetricsHtml({ pid: 1, memory_bytes: 104857600, cpu_percent: null }, "5");
check("an unmeasured CPU is left out, not shown as unknown", firstPoll.indexOf("CPU") === -1, true);
truthy("the memory still shows", firstPoll.indexOf("Memory") !== -1, firstPoll);
check("the PID is not in the metrics line", firstPoll.indexOf("PID") === -1, true);

const measured = performerMetricsHtml({ pid: 1, memory_bytes: 1024, cpu_percent: 42.25 }, "5");
truthy("a measured CPU shows its value", measured.indexOf("CPU 42.3%") !== -1, measured);
truthy("the CPU readout names its basis in a tooltip", measured.indexOf("since the previous poll") !== -1, measured);

const zeroMeasured = performerMetricsHtml({ pid: 1, memory_bytes: 1024, cpu_percent: 0 }, "5");
truthy("a measured zero is a real zero", zeroMeasured.indexOf("CPU 0.0%") !== -1, zeroMeasured);

// --- the technical block -------------------------------------------------------
const tech = performerTechnicalHtml({ pid: 1 }, "abcdef0123456789");
truthy("the PID moved to Technical", tech.indexOf("PID 1") !== -1 && tech.indexOf("<strong>Technical</strong>") !== -1, tech);
truthy("the container is in Technical", tech.indexOf("Container:") !== -1 && tech.indexOf("abcdef012345") !== -1, tech);
check("no technical facts means no technical block", performerTechnicalHtml(null, ""), "");

// --- the detail's container id ---------------------------------------------------
check("no container id is nothing, not a dash", containerIdHtml(""), "");

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
