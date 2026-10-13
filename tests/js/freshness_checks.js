// 152: checks for the header freshness text, executed against functions
// sliced out of the shipped helpers.js.
//
// Usage: node freshness_checks.js <path-to-sliced-js>

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node freshness_checks.js <sliced-js>");
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

const NOW = Date.parse("2026-10-13T12:00:30Z");
const state = { poll_interval_seconds: 30, last_poll_at: "2026-10-13T12:00:18Z" };

// Live: how old the data is and when the next check comes.
check("live shows age and the next check",
  freshnessText("Live", NOW - 3000, true, state, NOW), " · updated 3s ago · next check in 18s");
check("live without poll timing still shows age",
  freshnessText("Live", NOW - 3000, true, {}, NOW), " · updated 3s ago");
check("live with no state still shows age",
  freshnessText("Live", NOW - 1000, true, null, NOW), " · updated 1s ago");
check("an overdue check reads 0s, never negative",
  freshnessText("Live", NOW, true, { poll_interval_seconds: 5, last_poll_at: "2026-10-13T11:00:00Z" }, NOW),
  " · updated 0s ago · next check in 0s");

// Stale: say how long, and that it is reconnecting when it is.
check("quiet says how long there has been no update",
  freshnessText("Updates quiet", NOW - 125000, true, state, NOW), " · no update for 2m");
check("reconnecting says so",
  freshnessText("Reconnecting", NOW - 125000, true, state, NOW), " · no update for 2m: reconnecting…");
check("waiting uses the stale wording too",
  freshnessText("Waiting for updates", NOW - 5000, true, state, NOW), " · no update for 5s");
check("stale text never promises a next check",
  /next check/.test(freshnessText("Reconnecting", NOW - 60000, true, state, NOW)), false);

// Nothing received yet: nothing to claim.
check("before the first message there is no freshness claim",
  freshnessText("Connecting", 0, false, state, NOW), "");

// Age formatting.
check("seconds", fmtShortAge(59999), "59s");
check("minutes", fmtShortAge(60000), "1m");
check("hours and minutes", fmtShortAge(3 * 3600000 + 12 * 60000), "3h 12m");
check("negative and junk ages read as zero", [fmtShortAge(-5), fmtShortAge("x"), fmtShortAge(undefined)], ["0s", "0s", "0s"]);

// The once-a-second refresh writes the text and flags a stale stream.
var classes = {};
var detail = { textContent: "" };
var statusEl = { classList: { toggle: function(name, on) { classes[name] = on; } } };
var labelEl = { textContent: "Live" };
var document = { getElementById: function(id) {
  return { "connection-detail": detail, "connection-label": labelEl, "connection-status": statusEl }[id] || null;
} };
var _afHasMessage = true;
var _afLastMsgAt = Date.now() - 4000;
var _lastState = null;
refreshFreshness();
check("refresh writes the live text", /^ · updated [45]s ago$/.test(detail.textContent), true);
check("a live stream is not flagged stale", classes["conn-stale"], false);
labelEl.textContent = "Reconnecting";
refreshFreshness();
check("refresh writes the stale text", /no update for [45]s: reconnecting…$/.test(detail.textContent), true);
check("a reconnecting stream is flagged stale", classes["conn-stale"], true);
_afHasMessage = false;
refreshFreshness();
check("no message yet clears the text and the flag", [detail.textContent, classes["conn-stale"]], ["", false]);

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
