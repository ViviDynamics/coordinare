// 157: checks for the shared time helpers (fmtTime, fmtAgo, timeHtml), executed against functions
// sliced out of the shipped helpers.js. The zone-dependent line is checked against the expected
// text the caller passes in TIME_157_EXPECT, because the zone is set by the process's TZ.
//
// Usage: TZ=<zone> TIME_157_EXPECT="<text>" node time_157_checks.js <path-to-sliced-js>

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node time_157_checks.js <sliced-js>");
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

// The helpers touch no document until a page function runs, so the slice evaluates as is.
eval(src);

const NOW = Date.parse("2026-10-14T12:00:00Z");
const EXPECT = process.env.TIME_157_EXPECT || "";

// The exact time carries its zone. The expected text depends on the TZ this process runs in.
check("the exact time shows its zone label", fmtTime("2026-10-13T21:00:50+00:00"), EXPECT);
check("microseconds are read and dropped", fmtTime("2026-10-13T21:00:50.123456+00:00"), EXPECT);

// With no zone name, the time is shown in UTC and labelled UTC.
zoneLabel.formatter = null;
const realFormat = Intl.DateTimeFormat;
Intl.DateTimeFormat = function () { throw new Error("no Intl zone names"); };
check("a missing zone name falls back to UTC", zoneLabel(new Date(NOW)), "");
check("a missing zone name shows the time in UTC", fmtTime("2026-10-13T21:00:50+00:00"), "2026-10-13 21:00:50 UTC");
Intl.DateTimeFormat = realFormat;
zoneLabel.formatter = null;

// Relative times: seconds, minutes, hours, days, and a future moment never reads negative.
check("under a minute is seconds", fmtAgo("2026-10-14T11:59:25+00:00", NOW), "35s ago");
check("under an hour is minutes", fmtAgo("2026-10-14T11:57:00+00:00", NOW), "3m ago");
check("under a day is hours", fmtAgo("2026-10-14T09:00:00+00:00", NOW), "3h ago");
check("a day or more is days", fmtAgo("2026-10-12T12:00:00+00:00", NOW), "2d ago");
check("a future moment reads as now", fmtAgo("2026-10-14T12:00:30+00:00", NOW), "0s ago");

// The fallback: an absent or unreadable timestamp is "unknown", or the caller's own word.
check("an absent time is unknown", fmtTime(null), "unknown");
check("an unreadable time is unknown", fmtTime("not a time"), "unknown");
check("an absent age is empty", fmtAgo(null, NOW), "");
check("an unreadable age is empty", fmtAgo("not a time", NOW), "");
check("timeHtml falls back to unknown", timeHtml(null), "unknown");
check("timeHtml uses the caller's fallback", timeHtml(undefined, "-"), "-");

// timeHtml: the age is the text, the exact time is the title, and the age is marked for refresh.
const html = timeHtml("2026-10-13T21:00:50+00:00");
check("timeHtml is a time element", html.startsWith("<time "), true);
check("timeHtml carries the exact time in its title", html.includes('title="' + EXPECT + '"'), true);
check("timeHtml marks its source for the ticker", html.includes('data-rel-iso="2026-10-13T21:00:50+00:00"'), true);
const hostile = timeHtml('2026-10-13T21:00:50+00:00"><script>');
check("timeHtml escapes its attributes", hostile.includes("<script>"), false);

// A timestamp with no zone designator is UTC, as the daemon writes it: the same moment as the Z form,
// with or without the space between date and time.
check("a naive timestamp is the UTC moment", fmtTime("2026-10-13T21:00:50"), EXPECT);
check("a naive space-separated timestamp is the UTC moment", fmtTime("2026-10-13 21:00:50"), EXPECT);
check("a naive age is read from UTC", fmtAgo("2026-10-14T11:59:25", NOW), "35s ago");
// Epoch numbers are not timestamps here: they read as unreadable, documented in helpers.js.
check("an epoch number is unreadable for the time", fmtTime(1760389250), "unknown");
check("an epoch number is unreadable for the age", fmtAgo(1760389250, NOW), "");
// The old names are the one formatter, so no caller can drift from it.
check("fmtAge is fmtAgo", fmtAge("2026-10-14T11:57:00+00:00"), fmtAgo("2026-10-14T11:57:00+00:00"));
check("fmtAgeAt is fmtAgo at a moment", fmtAgeAt("2026-10-14T09:00:00+00:00", NOW), "3h ago");
// The zone formatter is rebuilt when the browser's time zone changes.
zoneLabel(new Date(NOW));
const before = zoneLabel.formatter;
const realZone = zoneLabel.zone;
zoneLabel.zone = "Not/AZone";
zoneLabel(new Date(NOW));
check("a changed time zone rebuilds the zone formatter", zoneLabel.formatter !== before, true);
check("the formatter remembers the zone it was built for", zoneLabel.zone === realZone, true);

// 157: the persona line. The author is what the history recorded; a blank or unknown author is never invented.
check("a blank author reads as unknown author", personaAuthorText(""), "unknown author");
check("an unknown author reads as unknown author", personaAuthorText("unknown"), "unknown author");
check("a recorded author is shown as recorded", personaAuthorText("Ada Lovelace"), "Ada Lovelace");
check("the shared token is named as the token", personaAuthorText("token"), "shared token");
const notRecorded = '<span class="muted">Last edited: not recorded</span>';
check("no history reads as not recorded", personaLastEditedHtml({ versions: [] }), notRecorded);
check("no history file reads as not recorded", personaLastEditedHtml(null), notRecorded);
const latest = personaLastEditedHtml({ versions: [
  { version: 1, changed_at: "2026-10-13T21:00:50+00:00", changed_by: "Ada" },
  { version: 3, changed_at: "2026-10-14T11:59:25+00:00", changed_by: "" },
  { version: 2, changed_at: "2026-10-13T22:00:00+00:00", changed_by: "Grace" },
] });
check("the newest version is the one shown", latest.includes('data-rel-iso="2026-10-14T11:59:25+00:00"'), true);
check("a newest author with no name is unknown author", latest.includes("by unknown author"), true);

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
