// 121: checks for the dashboard's shared cost estimate, executed against the
// functions the Python test slices out of the shipped performers.js.
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

const top = { cost_per_million_tokens: 3 };

// The daemon's own figure wins.
check("reported cost is used as is",
  estimateCost({ card_cost_estimate: 1.25, card_tokens_total: 10 }, top).value, 1.25);

// The bug: tokens on screen, cost 0. Estimate from the same tokens.
check("tokens with no reported cost are priced at the flat rate",
  estimateCost({ card_cost_estimate: 0, card_tokens_total: 382979 }, top).value, 382979 / 1000000 * 3);
check("live performer metrics count as tokens",
  estimateCost({ performer_metrics: { tokens_processed: 2000000 } }, top).value, 6);
check("a non-zero token count never yields a zero cost",
  estimateCost({ card_tokens_total: 1 }, top).value > 0, true);

// Nothing to estimate.
check("no tokens and no cost is zero", estimateCost({}, top).value, 0);
check("missing arguments do not throw", estimateCost(undefined, undefined).value, 0);
check("a zero rate cannot invent a cost", estimateCost({ card_tokens_total: 500 }, { cost_per_million_tokens: 0 }).value, 0);
check("an absent rate cannot invent a cost", estimateCost({ card_tokens_total: 500 }, {}).value, 0);

// The tooltip names the pricing basis.
check("basis names the rate",
  estimateCost({ card_tokens_total: 5 }, top).basis,
  "Estimate at $3.00 per million tokens (one flat rate for all models and token types)");
check("basis without a rate still says it is an estimate",
  /flat rate/.test(estimateCost({}, {}).basis), true);

// Rendered form carries the tooltip and the requested precision.
check("costHtml renders value and tooltip",
  costHtml({ card_tokens_total: 1000000 }, top, 4),
  '<span title="Estimate at $3.00 per million tokens (one flat rate for all models and token types)">$3.0000</span>');

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
