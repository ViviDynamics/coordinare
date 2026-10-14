// 134: checks for the dashboard's token and cost display (usage.js), executed
// against the sliced shipped source the Python test writes to disk.
//
// Usage: node usage_checks.js <path-to-sliced-js>
//
// Deliberately NOT strict mode: the shipped block is loaded with a direct eval.

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node usage_checks.js <sliced-js>");
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
function checkContains(name, actual, fragment) {
  if (String(actual).indexOf(fragment) !== -1) { passed += 1; } else {
    failed += 1;
    console.error(`FAIL ${name}: ${JSON.stringify(actual)} does not contain ${JSON.stringify(fragment)}`);
  }
}

eval(src);

// The summary the daemon sends for the #134 sample run, priced as a table.
const priced = {
  tracked: true,
  tokens: { input: 1050, output: 1000, cache_read: 17000, cache_write: 300, total: 19350 },
  cost: {
    usd: 0.125625,
    partial_usd: 0.125625,
    kind_usd: { input_usd: 0.01775, output_usd: 0.0755, cache_read_usd: 0.0255, cache_write_usd: 0.006875 },
    unknown_reason: null,
  },
  models: [
    { model: "claude-opus-4-5", tokens: { input: 1050, output: 1000, cache_read: 17000, cache_write: 300, total: 19350 }, priced: true, cost_usd: 0.121875, reason: null },
  ],
  pricing: { as_of: "2026-10-01", source: "table" },
};

const unknown = {
  tracked: true,
  tokens: { input: 1500000, output: 0, cache_read: 0, cache_write: 0, total: 1500000 },
  cost: { usd: null, partial_usd: 0, kind_usd: {}, unknown_reason: "no price for gpt-unpriced" },
  models: [{ model: "gpt-unpriced", tokens: { input: 1500000, output: 0, cache_read: 0, cache_write: 0, total: 1500000 }, priced: false, cost_usd: null, reason: "no price configured" }],
  pricing: { as_of: "2026-10-01", source: "table" },
};

const untracked = { tracked: false, reason: "transcripts disabled", tokens: { total: 4200 } };

// Integers: thousands separators, no locale dependence.
check("counts get separators", usageCount(1500000), "1,500,000");
check("small counts are plain", usageCount(42), "42");
check("zero is zero", usageCount(0), "0");
check("junk counts read as zero", usageCount("x"), "0");

// Headline cost: the same two-decimal format as every other cost.
const readout = usageCostReadout(priced);
check("priced cost text", readout.text, "$0.13");
check("priced cost is not unknown", readout.unknown, false);
checkContains("tooltip names the pricing date", readout.title, "Pricing as of 2026-10-01.");
checkContains("tooltip names the input count", readout.title, "input 1,050");
checkContains("tooltip names the cache read count", readout.title, "cache read 17,000");
checkContains("tooltip names the cache write count", readout.title, "cache write 300");
checkContains("tooltip gives the exact total", readout.title, "$0.125625");
checkContains("tooltip names the model", readout.title, "claude-opus-4-5");

// Unknown price: never a number, always the reason.
const unknownReadout = usageCostReadout(unknown);
check("unknown cost text", unknownReadout.text, "unknown");
check("unknown cost is flagged", unknownReadout.unknown, true);
checkContains("unknown reason is on the tooltip", unknownReadout.title, "no price for gpt-unpriced");
checkContains("unknown model is named", unknownReadout.title, "gpt-unpriced: 1,500,000 tokens, no price (no price configured)");

// Transcripts disabled: not tracked, never zero.
const offReadout = usageCostReadout(untracked);
check("untracked cost text", offReadout.text, "not tracked");
checkContains("untracked reason on the tooltip", offReadout.title, "transcripts disabled");
check("untracked tokens say total only", usageTokensReadout(untracked).text, "4,200 tokens (total only)");
check("tracked tokens", usageTokensReadout(priced).text, "19,350 tokens");

// No summary at all is not a zero either.
check("missing summary is not tracked", usageCostReadout(null).text, "not tracked");

// Pricing basis lines.
checkContains("flat rate basis is named", usageBreakdownText({ tracked: true, tokens: { total: 1 }, cost: { usd: 0.1, kind_usd: {} }, models: [], pricing: { as_of: null, source: "deprecated_flat_rate" } }), "deprecated flat rate, no pricing date");
checkContains("no pricing basis is named", usageBreakdownText({ tracked: true, tokens: { total: 1 }, cost: { usd: null, kind_usd: {} }, models: [], pricing: { as_of: null, source: "none" } }), "no price configured");

// Spend strip: today and the rolling week, both UTC.
const block = { tracked: true, day: "2026-10-14", today: priced, week: unknown, pricing: priced.pricing };
check("spend strip text", spendStripText(block), "Spend today $0.13 · this week unknown");
checkContains("spend strip says UTC", spendStripTitle(block), "UTC");
check("untracked spend strip", spendStripText({ tracked: false, reason: "transcripts disabled" }), "Spend not tracked");
check("missing spend strip", spendStripText(null), "Spend not tracked");

// Markup: every interpolated value goes through esc().
const hostile = { tracked: true, tokens: { total: 1 }, cost: { usd: 0.5, kind_usd: {} }, models: [{ model: "<img src=x>", tokens: { total: 1 }, priced: true, cost_usd: 0.5, reason: null }], pricing: { as_of: "2026-10-01", source: "table" } };
const html = usageCostHtml(hostile);
check("cost markup is a span", /^<span title="/.test(html), true);
check("model names are escaped in the tooltip", html.indexOf("<img") === -1, true);
check("markup carries the text", /\$0\.50<\/span>$/.test(html), true);

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
