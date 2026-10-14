// 211: checks for the activity link's target and the run header's ledger figures, executed
// against the shipped helpers.js (esc, fmtCost), usage.js and runs.js.
//
// Usage: node transcript_link_checks.js <path-to-sliced-js>

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node transcript_link_checks.js <sliced-js>");
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

const SESSION = "run-211";
const AT = "2026-10-01T10:00:04.250000+00:00";

function entry(extra) {
  return Object.assign({ session_id: SESSION, event_at: AT, timestamp: AT }, extra || {});
}

// --- the activity link's target ------------------------------------------------------
has("an exact seq links to that record's fragment", runEntryLinkHtml(entry({ transcript_seq: 42 })),
  'href="/runs/run-211#e42"');
lacks("an exact seq does not also send the time", runEntryLinkHtml(entry({ transcript_seq: 42 })), "?at=");
has("no seq falls back to the event time", runEntryLinkHtml(entry({ transcript_seq: null })),
  'href="/runs/run-211?at=');
has("a missing seq key falls back to the event time", runEntryLinkHtml(entry()), "?at=2026-10-01T10%3A00%3A04.250000%2B00%3A00");
for (const bad of [0, -1, 1.5, "42", true, Number.MAX_SAFE_INTEGER + 2, NaN]) {
  lacks(`an invalid seq ${JSON.stringify(String(bad))} is not used`, runEntryLinkHtml(entry({ transcript_seq: bad })), "#e");
}
has("the largest safe seq is still exact", runEntryLinkHtml(entry({ transcript_seq: Number.MAX_SAFE_INTEGER })),
  `#e${Number.MAX_SAFE_INTEGER}`);
check("an entry with no session has no link", runEntryLinkHtml(entry({ session_id: "", transcript_seq: 7 })), "");
check("a session that is not a run name has no link", runEntryLinkHtml(entry({ session_id: "../x", transcript_seq: 7 })), "");
has("the link names the run by its short id (141)", runEntryLinkHtml(entry({ transcript_seq: 3 })), "run-21");

// --- the run header's ledger figures -----------------------------------------------------
const TRACKED = {
  tracked: true, reason: null,
  tokens: { input: 1050, output: 1000, cache_read: 17000, cache_write: 300, total: 19350 },
  cost: {
    usd: 0.125625, partial_usd: 0.125625,
    kind_usd: { input_usd: 0.01775, output_usd: 0.0755, cache_read_usd: 0.0255, cache_write_usd: 0.006875 },
    unknown_reason: null,
  },
  models: [{
    model: "claude-opus-4-5", tokens: { input: 1050, output: 1000, cache_read: 17000, cache_write: 300, total: 19350 },
    priced: true, price: { input_per_million: 15, output_per_million: 75, cache_read_per_million: 1.5, cache_write_per_million: 18.75 },
    cost_usd: 0.121875, reason: null,
  }],
  pricing: { as_of: "2026-10-01", source: "table" },
};
const UNPRICED = {
  tracked: true, reason: null,
  tokens: { input: 500000, output: 0, cache_read: 0, cache_write: 0, total: 500000 },
  cost: { usd: null, partial_usd: 0, kind_usd: {}, unknown_reason: "no price for <b>gpt-x</b> (no price configured)" },
  models: [{
    model: "<b>gpt-x</b>", tokens: { input: 500000, output: 0, cache_read: 0, cache_write: 0, total: 500000 },
    priced: false, price: null, cost_usd: null, reason: "no price configured",
  }],
  pricing: { as_of: "2026-10-01", source: "table" },
};

function header(usage) {
  return runHeaderHtml({
    symphony: "coordinare", card: "PVTI_211", run: SESSION,
    header_summary: { stage: "implementing", agent: "claude", model: "claude-opus-4-5" },
    summary: { usage: usage, outcome: { status: "completed" }, duration_seconds: 12 },
  });
}

const priced = header(TRACKED);
has("the header shows the ledger cost", priced, '<span title="');
has("the cost reads as a dollar figure", priced, "$0.13");
has("the tokens read as the ledger total", priced, "19,350 tokens");
has("the cost tooltip has the breakdown by kind", priced, "Cost: input $0.017750, output $0.075500");
has("the cost tooltip has the pricing date", priced, "Pricing as of 2026-10-01.");
has("the tokens tooltip has the cache split", priced, "cache read 17,000, cache write 300");
has("the tooltip names the model's price line", priced, "claude-opus-4-5: 19,350 tokens, $0.121875");

const unknown = header(UNPRICED);
has("an unpriced model reads unknown", unknown, ">unknown</span>");
has("the unknown cost gives its reason in the tooltip", unknown, "Cost unknown: no price for &lt;b&gt;gpt-x&lt;/b&gt; (no price configured)");
lacks("the unknown cost shows no dollar figure", unknown, "$0.");
lacks("a hostile model name is escaped", unknown, "<b>gpt-x</b>");

const untracked = header({ tracked: false, reason: "the ledger holds no usage for this run" });
has("an untracked run reads not tracked for tokens", untracked, ">not tracked</span>");
has("an untracked run gives its reason", untracked, "the ledger holds no usage for this run");
has("an untracked run reads not tracked for cost", untracked, "Cost</span><span title=");

const absent = header(undefined);
has("a header with no usage block is not tracked", absent, ">not tracked</span>");

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
