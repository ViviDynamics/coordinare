// 354: checks for the dashboard's queuedPhaseLabel helper, executed against
// the JS file the Python test slices out of _DASHBOARD_HTML.
//
// Usage: node queued_board_checks.js <path-to-sliced-js>
//
// Deliberately NOT strict mode: the shipped block is loaded with a direct eval,
// and a strict-mode eval would give the loaded declarations their own scope.

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node queued_board_checks.js <sliced-js>");
  process.exit(1);
}
const src = fs.readFileSync(path.resolve(jsPath), "utf8");

let passed = 0;
let failed = 0;
function check(name, actual, expected) {
  const ok = actual === expected;
  if (ok) { passed += 1; } else {
    failed += 1;
    console.error(`FAIL ${name}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

eval(src);

// A dispatching-phase card carrying the marker is queued for its stage.
check(
  "queued dispatching card renders queued label",
  queuedPhaseLabel({ phase: "dispatching", performer_stage: "implementing", slot_queued_since: "2026-09-23T11:48:00Z" }),
  "Queued for Implementing",
);

// Stage label is title-cased through the shared formatter (assessing, etc).
check(
  "queued label uses the formatted stage name",
  queuedPhaseLabel({ phase: "dispatching", performer_stage: "assessing", slot_queued_since: "2026-09-23T11:48:00Z" }),
  "Queued for Assessing",
);

// A card with no marker is NOT queued — it keeps its ordinary label path.
check(
  "dispatching card without marker is not queued",
  queuedPhaseLabel({ phase: "dispatching", performer_stage: "implementing" }),
  null,
);

// Non-dispatching phases never render the queued label, even with a stale marker.
check(
  "monitoring card with stale marker is not queued",
  queuedPhaseLabel({ phase: "monitoring_performer", performer_stage: "implementing", slot_queued_since: "2026-09-23T11:48:00Z" }),
  null,
);

// Missing stage falls back to a generic noun rather than "undefined".
check(
  "queued label without stage is generic",
  queuedPhaseLabel({ phase: "dispatching", slot_queued_since: "2026-09-23T11:48:00Z" }),
  "Queued for performer",
);

// Null/undefined sessions are refused outright.
check("null session is not queued", queuedPhaseLabel(null), null);

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
