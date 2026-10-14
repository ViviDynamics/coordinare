// 121 / 134: checks for the dashboard's card cost, executed against the functions
// the Python test slices out of the shipped performers.js and usage.js.
//
// 134 replaced the flat-rate estimate: the cost is the daemon's priced usage
// summary, and nothing is computed from a token count here.
//
// Usage: node cost_estimate_checks.js <path-to-sliced-js>
//
// Deliberately NOT strict mode: the shipped block is loaded with a direct eval.

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node cost_estimate_checks.js <sliced-js>");
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

eval(src);

// A usage summary as the daemon sends it, with the given tokens and priced cost.
function summary(tokens, usd) {
  return {
    tokens: { input: tokens, output: 0, cache_read: 0, cache_write: 0, total: tokens },
    cost: { usd: usd, partial_usd: usd, kind_usd: {}, unknown_reason: null },
    models: [],
    pricing: { as_of: "2026-10-01", source: "table" },
  };
}
function sessionWith(card, phase) {
  return { phase: phase, usage: { tracked: true, run: card, card: card } };
}

// The daemon's own priced figure is what is shown.
check("the priced card cost is used as is",
  estimateCost(sessionWith(summary(1000000, 3), "monitoring_pr"), {}).value, 3);
check("the estimate carries the breakdown as its basis",
  /Pricing as of 2026-10-01\./.test(estimateCost(sessionWith(summary(1000000, 3), "monitoring_pr"), {}).basis), true);

// Nothing priced is never a made-up number.
check("no usage summary is zero", estimateCost({}, {}).value, 0);
check("missing arguments do not throw", estimateCost(undefined, undefined).value, 0);
check("an unknown cost is zero, not a guess",
  estimateCost({ usage: { tracked: true, card: { tokens: { total: 5 }, cost: { usd: null, unknown_reason: "no price for m" }, models: [] } } }, {}).value, 0);
check("untracked transcripts price nothing",
  estimateCost({ usage: { tracked: false, reason: "transcripts disabled" } }, {}).value, 0);

// Rendered form carries the tooltip and the two-decimal format.
check("costHtml renders value and tooltip",
  costHtml(sessionWith(summary(1000000, 3), "monitoring_pr"), {}),
  '<span title="Tokens: input 1,000,000, output 0, cache read 0, cache write 0 (total 1,000,000)\nCost: input $0, output $0, cache read $0, cache write $0 (total $3.000000)\nPricing as of 2026-10-01.">$3.00</span>');

// Untracked: not tracked, and the reason is on the tooltip.
check("costHtml for untracked transcripts says not tracked",
  costHtml({ usage: { tracked: false, reason: "transcripts disabled" } }, {}).indexOf(">not tracked</span>") !== -1, true);
check("costHtml with no usage says not tracked",
  costHtml({}, {}).indexOf(">not tracked</span>") !== -1, true);

// 144: one format everywhere: two decimals, three when under a cent.
check("ordinary amounts use two decimals", fmtCost(1.149), "$1.15");
check("amounts under a cent use three decimals", fmtCost(0.0042), "$0.004");
check("exactly a cent uses two decimals", fmtCost(0.01), "$0.01");
check("zero, negative and junk render as $0.00", [fmtCost(0), fmtCost(-1), fmtCost("x"), fmtCost(undefined)], ["$0.00", "$0.00", "$0.00", "$0.00"]);

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
