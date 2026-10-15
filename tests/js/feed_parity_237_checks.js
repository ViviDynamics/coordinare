// 237: runs every fixture case through the shipped afEntryFromBackend and prints the results as JSON,
// for tests/unit/test_237_feed_parity.py to compare with the daemon's _feed_fields.
//
// Usage: node feed_parity_237_checks.js <sliced-activity-streams-js> <fixture-json>

const fs = require("fs");
const path = require("path");

const sourcePath = process.argv[2];
const fixturePath = process.argv[3];
if (!sourcePath || !fixturePath) {
  console.error("usage: node feed_parity_237_checks.js <sliced-js> <fixture-json>");
  process.exit(1);
}
const src = fs.readFileSync(path.resolve(sourcePath), "utf8");
const fixture = JSON.parse(fs.readFileSync(path.resolve(fixturePath), "utf8"));

// The chip preferences load at script start and read localStorage, so a stub stands in for it.
global.window = { localStorage: { getItem() { return null; }, setItem() {} } };

eval(src);

const results = fixture.cases.map(function(c) {
  const e = afEntryFromBackend(Object.assign({ is_delta: false }, c.event), "");
  return { id: c.id, activity_type: e.activity_type, summary: e.summary, status: e.status };
});
process.stdout.write(JSON.stringify(results));
