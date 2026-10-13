// 122: checks for the token and cost pending state, executed against the
// functions the Python test slices out of the shipped performers.js.
//
// Usage: node usage_pending_checks.js <path-to-sliced-js>

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node usage_pending_checks.js <sliced-js>");
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

const NOTE = "Reported when the stage ends";
const top = { cost_per_million_tokens: 3 };

// A running stage with nothing reported is pending, never 0.
check("running with zero tokens is pending", tokenReadout(0, true), { text: "—", title: NOTE, pending: true });
check("running with unknown tokens is pending", tokenReadout(null, true), { text: "—", title: NOTE, pending: true });
check("running with NaN tokens is pending", tokenReadout(NaN, true).pending, true);
check("pending never renders the digit 0", /0/.test(tokenReadout(0, true).text), false);

// Reported usage is shown as soon as there is any, running or not.
check("running with tokens shows the count", tokenReadout(1234, true), { text: (1234).toLocaleString(), title: "", pending: false });
check("finished with tokens shows the count", tokenReadout(382979, false).text, (382979).toLocaleString());
check("fractional counts are floored", tokenReadout(12.9, true).text, "12");

// Nothing running: a true zero is a zero.
check("idle with zero tokens is a real 0", tokenReadout(0, false), { text: "0", title: "", pending: false });

// Which phases count as a running stage.
check("monitoring_performer is running", isStageRunning({ phase: "monitoring_performer" }), true);
check("monitoring_agent is running", isStageRunning({ phase: "monitoring_agent" }), true);
["dispatching", "monitoring_pr", "blocked", "idle", "merging", undefined].forEach(function(phase) {
  check("phase " + phase + " is not a running stage", isStageRunning({ phase: phase }), false);
});
check("missing session is not running", isStageRunning(undefined), false);

// Cost follows the same rule.
check("cost for a running stage with no usage is pending",
  costHtml({ phase: "monitoring_performer", card_tokens_total: 0 }, top), '<span title="' + NOTE + '">—</span>');
check("cost for a running stage with usage is priced",
  costHtml({ phase: "monitoring_performer", card_tokens_total: 1000000 }, top).indexOf("$3.00<") !== -1, true);
check("cost for a stage that is not running shows a real zero",
  costHtml({ phase: "monitoring_pr", card_tokens_total: 0 }, top).indexOf("$0.00<") !== -1, true);

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
