// 138, 140, 144: checks for the conductor headline and the small shared
// formatters, executed against functions sliced out of the shipped helpers.js.
//
// Usage: node naming_batch_checks.js <path-to-sliced-js>

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node naming_batch_checks.js <sliced-js>");
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

// --- 138: the conductor headline is a plain sentence -------------------------
check("watching an agent on a card",
  conductorHeadline({ phase: "monitoring_performer" }, { performer_stage: "implementing", issue_number: 57 }),
  "Watching Implementer on #57");
check("watching names the card by title when it has no number",
  conductorHeadline({}, { performer_stage: "qa", card_title: "Fix login" }), "Watching QA on Fix login");
check("watching with an unknown stage still reads as a sentence",
  conductorHeadline({}, { performer_stage: "translating", issue_number: 3 }), "Watching an agent on #3");
check("dispatching names the card and the agent",
  conductorHeadline({ phase: "dispatching", active_sessions: [{ phase: "dispatching", performer_stage: "assessing", issue_number: 65 }] }, null),
  "Dispatching #65 to Assessor");
var soon = new Date(Date.now() - 12000).toISOString();
check("idle counts down to the next check",
  conductorHeadline({ phase: "idle", poll_interval_seconds: 30, last_poll_at: soon }, null), "Idle: next check in 18s");
check("idle without timing says it is waiting",
  conductorHeadline({ phase: "idle" }, null), "Idle: waiting for the next check");
check("an overdue check never shows a negative countdown",
  conductorHeadline({ phase: "idle", poll_interval_seconds: 30, last_poll_at: "2020-01-01T00:00:00Z" }, null), "Idle: next check in 0s");
check("other states use the plain status word",
  conductorHeadline({ phase: "monitoring_pr" }, null), "Waiting on review");
check("the server's label wins when the snapshot carries one",
  conductorHeadline({ phase: "merging", phase_label: "Merging" }, null), "Merging");
check("no internal code leaks into the headline",
  /monitoring_|_performer|_agent/.test(conductorHeadline({ phase: "monitoring_performer" }, { performer_stage: "reviewing", issue_number: 1 })), false);
check("missing state does not throw", conductorHeadline(undefined, null), "Idle: waiting for the next check");

check("next check: unknown interval", nextCheckSeconds({ last_poll_at: soon }), null);
check("next check: zero interval", nextCheckSeconds({ poll_interval_seconds: 0, last_poll_at: soon }), null);
check("next check: never polled", nextCheckSeconds({ poll_interval_seconds: 30 }), null);

// --- 144: "1 of 1 busy" -------------------------------------------------------
check("busy with nothing queued", busyText(1, 1, 0), "1 of 1 busy");
check("busy with a queue", busyText(1, 1, 2), "1 of 1 busy · 2 queued");
check("idle role", busyText(0, 2), "0 of 2 busy");
check("missing numbers read as zero", busyText(undefined, undefined, undefined), "0 of 0 busy");
check("the word idle is gone", /idle/i.test(busyText(1, 3, 0)), false);

// --- 144: one cost format -----------------------------------------------------
check("two decimals normally", fmtCost(12.345), "$12.35");
check("three decimals under a cent", fmtCost(0.0042), "$0.004");

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
