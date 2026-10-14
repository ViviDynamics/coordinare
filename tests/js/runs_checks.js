// 133: checks for the transcript viewer's pure helpers, executed against the shipped
// runs.js. Covers the addresses, the escaping of hostile transcript text, the tool row
// text, the filters, the virtual window and the card grouping.
//
// Usage: node runs_checks.js <path-to-sliced-js>

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node runs_checks.js <sliced-js>");
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

// The helpers touch no document until a page function runs, so the file evaluates as is.
eval(src);

const T0 = "2026-10-01T10:00:00.000000+00:00";
const T1 = "2026-10-01T10:00:02.300000+00:00";

// --- addresses ------------------------------------------------------------------
check("a full address", runAddressFromPath("/runs/coordinare/PVTI_1/run-9"),
  { kind: "full", symphony: "coordinare", card: "PVTI_1", run: "run-9" });
check("a short address", runAddressFromPath("/runs/run-9"), { kind: "short", run: "run-9" });
check("a trailing slash is ignored", runAddressFromPath("/runs/run-9/").kind, "short");
check("two segments are not an address", runAddressFromPath("/runs/a/b").invalid, true);
check("a dot-dot segment is not an address", runAddressFromPath("/runs/..").invalid, true);
check("an encoded slash is not an address", runAddressFromPath("/runs/a%2Fb").invalid, true);
check("a malformed segment is not an address", runAddressFromPath("/runs/%E0%A4%A").invalid, true);
check("a non-runs path is not an address", runAddressFromPath("/performers/x").invalid, true);
check("runs paths are recognised", isRunsPath("/runs/x"), true);
check("similar prefixes are not", isRunsPath("/runsx"), false);
check("a path is built with encoded segments", runPathFor("a b", "c", "r"), "/runs/a%20b/c/r");
check("the event hash", runEventHash(12), "#e12");
check("the seq is read from a hash", runSeqFromHash("#e12"), 12);
check("another fragment has no seq", runSeqFromHash("#x12"), null);
check("the time parameter is decoded", runQueryParam("?at=2026-10-01T10%3A00%3A05%2B00%3A00", "at"),
  "2026-10-01T10:00:05+00:00");
check("an absent parameter is empty", runQueryParam("?x=1", "at"), "");
check("the copy link is the run with its event hash", runCopyLinkUrl("https://h", "s", "c", "r", 7),
  "https://h/runs/s/c/r#e7");

// --- links -----------------------------------------------------------------------
check("an https link is kept", runsSafeHref("https://github.com/o/r/pull/9"), "https://github.com/o/r/pull/9");
check("a javascript link is dropped", runsSafeHref("javascript:alert(1)"), "");
check("a data link is dropped", runsSafeHref("data:text/html,<script>1</script>"), "");
check("a missing link is empty", runsSafeHref(null), "");
const entryLink = runEntryLinkHtml({ session_id: "abc123", timestamp: "2026-10-01T10:00:05+00:00" });
has("an activity entry links to its run", entryLink, 'href="/runs/abc123?at=2026-10-01T10%3A00%3A05%2B00%3A00"');
check("an entry without a run gets no link", runEntryLinkHtml({ session_id: "" }), "");
check("a hostile run id gets no link", runEntryLinkHtml({ session_id: '"><script>x</script>' }), "");
has("the session link opens the transcript", runSessionLinkHtml("run-7"), 'href="/runs/run-7"');
check("a hostile session id gets no link", runSessionLinkHtml('a"b'), "");
check("a persona history path uses an integer version", personaHistoryPath("implementing", 3),
  "/api/personas/implementing/history/3");
check("a fractional version has no path", personaHistoryPath("implementing", 1.5), "");

// --- escaping of transcript text (the hostile fixture) -------------------------
const HOSTILE = '<script>alert(1)</script><img src=x onerror="alert(2)">';
has("prose is escaped, not markup", textToHtml(HOSTILE), "&lt;script&gt;");
lacks("prose has no live script", textToHtml(HOSTILE), "<script");
lacks("prose has no live image", textToHtml(HOSTILE), "<img");
check("line breaks are kept as breaks", textToHtml("a\nb\r\nc"), "a<br>b<br>c");

const hostileToolCall = {
  type: "tool_call", seq: 4, line: 5, tool_name: HOSTILE, arg: HOSTILE, ts: T0, call_id: "c1",
};
const toolRow = runRowHtml(hostileToolCall, { open: {}, calls: {}, results: {} });
lacks("a hostile tool name is not markup", toolRow, "<script");
lacks("a hostile tool name is not an image", toolRow, "<img");
has("a hostile tool name is shown as text", toolRow, "&lt;script&gt;");

const hostileAssistant = { type: "assistant_text", seq: 2, line: 3, text: HOSTILE, text_truncated: false };
const assistantRow = runRowHtml(hostileAssistant, { open: {}, calls: {}, results: {} });
lacks("hostile assistant text is not markup", assistantRow, "<script");
has("hostile assistant text is escaped", assistantRow, "&lt;img");

const hostileUnreadable = { unreadable: true, line: 9, bytes: 40, preview: HOSTILE };
const unreadableRow = runRowHtml(hostileUnreadable, { open: {}, calls: {}, results: {} });
lacks("an unreadable line's text is not markup", unreadableRow, "<script");
has("an unreadable line says so", unreadableRow, "could not be read");
has("an unreadable line keeps its line number", unreadableRow, "line 9");

const hostileCard = runCardRunsHtml([{ symphony: "s", run: HOSTILE, stage: HOSTILE, started_at: HOSTILE }], "PVTI_1");
lacks("a hostile run id in the card list is not markup", hostileCard, "<img");
lacks("a hostile stage name is not markup", hostileCard, "<script");

const hostileDetail = runHeaderHtml({
  symphony: "s", card: "c", run: "r",
  header_summary: { stage: HOSTILE, agent: HOSTILE, persona: { name: HOSTILE, version: 2 } },
  summary: { duration_seconds: 1, tokens: {}, outcome: { status: "completed" }, pr_url: "javascript:alert(3)", pr_reason: "none" },
});
lacks("a hostile header is not markup", hostileDetail, "<script");
lacks("a javascript pr link is not an anchor", hostileDetail, 'href="javascript');

// --- tool rows and results -------------------------------------------------------
check("a tool row names the call, its exit and its time",
  toolRowText({ tool_name: "Bash", arg: "pytest -q tests/", ts: T0 },
    { exit_code: 1, is_error: true, ts: T1 }),
  "Bash · pytest -q tests/ · exit 1 · 2.3s");
check("a call whose result is not loaded shows only its name and argument",
  toolRowText({ tool_name: "Bash", arg: "ls", ts: T0 }, null), "Bash · ls");
check("a failed result without an exit code says error",
  toolRowText({ tool_name: "Edit", arg: "a.py", ts: T0 }, { is_error: true, ts: T1 }), "Edit · a.py · error · 2.3s");
check("a result with no exit code and no error says ok",
  resultRowText({ is_error: false, ts: T1 }, { ts: T0 }), "result · ok · 2.3s");
check("a result's exit code is shown", resultRowText({ exit_code: 0, ts: T1 }, null), "result · exit 0");

const toolWithResult = runRowHtml(
  { type: "tool_call", seq: 4, line: 5, tool_name: "Bash", arg: "pytest -q tests/", call_id: "c1", ts: T0, input: "{}" },
  { open: {}, calls: {}, results: { c1: { exit_code: 1, is_error: true, ts: T1 } } },
);
has("a tool row in the window shows its outcome", toolWithResult, "Bash · pytest -q tests/ · exit 1 · 2.3s");

// --- thinking, lazy output and the open state -------------------------------------
const thinking = runRowHtml(
  { type: "thinking", seq: 3, line: 4, text: "plan", text_truncated: false, text_bytes: 4 },
  { open: {}, calls: {}, results: {} },
);
has("thinking is a collapsed details block", thinking, '<details class="run-think" data-line="4">');
const thinkingOpen = runRowHtml(
  { type: "thinking", seq: 3, line: 4, text: "plan", text_truncated: false, text_bytes: 4 },
  { open: { 4: true }, calls: {}, results: {} },
);
has("an opened row stays open across a redraw", thinkingOpen, '<details class="run-think" open data-line="4">');

const big = runRowHtml(
  { type: "tool_result", seq: 5, line: 6, call_id: "c1", is_error: true, output: "x", output_truncated: true, output_bytes: 9000, output_format: "text" },
  { open: {}, calls: {}, results: {} },
);
has("a long output offers its full value on demand", big, 'data-action="full" data-line="6" data-field="output"');
has("the full-value button says how big it is", big, "9000 bytes");
has("a result row names its outcome", big, "result · error");

const copy = runRowHtml({ type: "user", seq: 1, line: 2, text: "hi" }, { open: {}, calls: {}, results: {} });
has("every event has a copy-link control", copy, 'data-action="copy" data-seq="1"');
has("an event row has the anchor the fragment names", copy, 'id="e1"');

// --- filters ---------------------------------------------------------------------
const records = [
  { seq: 1, line: 2, type: "user" },
  { seq: 2, line: 3, type: "tool_call" },
  { seq: 3, line: 4, type: "tool_result", is_error: true },
  { line: 5, unreadable: true, type: "unreadable" },
  { seq: 4, line: 6, type: "error" },
];
check("no filter keeps everything", filterRecords(records, {}).length, 5);
check("a type filter keeps that type",
  filterRecords(records, { types: ["tool_call"] }).map(function(r) { return r.seq; }), [2]);
check("the errors filter keeps failures and unreadable lines",
  filterRecords(records, { errors: true }).map(function(r) { return r.line; }), [4, 5, 6]);
check("an unreadable line is hidden by a type filter alone",
  filterRecords(records, { types: ["user"] }).some(function(r) { return r.unreadable; }), false);
// An unreadable line (seq null) stays under the errors filter, as it does on the server.
check("errors narrow a type filter",
  filterRecords(records, { types: ["tool_result", "user"], errors: true }).map(function(r) { return r.seq; }), [3, null]);

// --- the virtual window ----------------------------------------------------------
function heights(n, h) {
  var out = [];
  for (var i = 0; i < n; i++) out.push(h);
  return out;
}
var top = windowFor(heights(1000, 100), 0, 300, 2);
check("the first window starts at the top", top.start, 0);
check("the first window covers the viewport and a margin", top.end, 5);
check("nothing is above the first window", top.padTop, 0);
check("the rest is padding below", top.padBottom, (1000 - 5) * 100);
var mid = windowFor(heights(1000, 100), 5000, 300, 0);
check("a window in the middle starts on its row", mid.start, 50);
check("a window in the middle ends after the viewport", mid.end, 53);
check("the space above is the rows skipped", mid.padTop, 5000);
check("the space below is the rows not drawn", mid.padBottom, (1000 - 53) * 100);
var tall = [100, 300, 100, 100];
var variable = windowFor(tall, 150, 100, 0);
check("a tall row is measured, not assumed", variable.start, 1);
check("the window stops at the viewport end", variable.end, 2);
var empty = windowFor([], 0, 300, 2);
check("an empty list has an empty window", [empty.start, empty.end, empty.padTop, empty.padBottom], [0, 0, 0, 0]);

// --- the page's address for a short link, and the runs grouping -------------------
check("runs group by stage in first-seen order",
  groupRunsByStage([{ stage: "implementing", run: "a" }, { stage: "reviewing", run: "b" }, { stage: "implementing", run: "c" }])
    .map(function(g) { return [g.stage, g.runs.length]; }),
  [["implementing", 2], ["reviewing", 1]]);
check("a run with no stage groups under unknown", groupRunsByStage([{ run: "x" }])[0].stage, "unknown stage");

// --- usage and durations ---------------------------------------------------------
check("usage reads as in, out and cache", usageLine({ input_tokens: 100, output_tokens: 20, cache_read: 5, cache_write: 2 }),
  "in 100 · out 20 · cache 5 read, 2 write");
check("usage with nothing says so", usageLine(null), "no usage");
check("durations under a minute keep a decimal", fmtRunSeconds(2.34), "2.3s");
check("durations over a minute say minutes", fmtRunSeconds(75), "1m 15s");
check("a missing duration is a dash", fmtRunSeconds(null), "-");
check("a duration from two timestamps", runSecondsBetween(T0, T1), 2.3);
check("a backwards pair has no duration", runSecondsBetween(T1, T0), null);

// --- the address of the run's download links -------------------------------------
has("the JSONL download is the run's API file", runDownloadHref({ symphony: "s", card: "c", run: "r" }, "jsonl"),
  "/api/runs/s/c/r/download.jsonl");

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
