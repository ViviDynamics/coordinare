// 151 review: the System health card is shared, so a viewer scoped to one symphony must
// never see another symphony's name in it. This loads the shipped symphonies.js verbatim,
// scopes a snapshot the Python side built to one symphony, and checks what survives.
//
// Usage: node system_health_scope_checks.js <path-to-symphonies.js> <path-to-snapshot.json>

const fs = require("fs");
const vm = require("vm");

const symphoniesPath = process.argv[2];
const snapshotPath = process.argv[3];
if (!symphoniesPath || !snapshotPath) {
  console.error("usage: node system_health_scope_checks.js <symphonies.js> <snapshot.json>");
  process.exit(1);
}

let passed = 0;
let failed = 0;
function check(name, ok, detail) {
  if (ok) { passed += 1; } else {
    failed += 1;
    console.error(`FAIL ${name}: ${detail || ""}`);
  }
}

// symphonies.js only defines functions at load time; the stubs cover what the page provides.
const ctx = {
  document: { getElementById: () => null, querySelectorAll: () => [] },
  window: {},
  location: { search: "", pathname: "/", hash: "" },
  history: { replaceState() {} },
  localStorage: { getItem() { return null; }, setItem() {} },
  console,
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(symphoniesPath, "utf8") + "\n;this.__scope = symphonyScope;", ctx);

const snapshot = JSON.parse(fs.readFileSync(snapshotPath, "utf8"));
const original = JSON.stringify(snapshot.system_health);
const scoped = ctx.__scope(snapshot, "beta");
const shared = JSON.stringify(scoped.system_health);

check("the beta view keeps the shared system_health row set", scoped.system_health && scoped.system_health.rows.length === snapshot.system_health.rows.length,
  `rows ${scoped.system_health && scoped.system_health.rows.length}`);
check("the beta view carries no alpha name in the shared card", shared.indexOf("alpha") === -1, shared);
// 266: the env reason is written for the chosen symphony; every other row of the card passes through unchanged.
const otherRows = (health) => JSON.stringify((health.rows || []).filter((row) => row.key !== "disk_env_cache"));
check("the other rows of the card are passed through unchanged",
  otherRows(scoped.system_health) === otherRows(snapshot.system_health), "scope rewrote another row");
check("the beta view names only its own failure, or none", original.indexOf("alpha") !== -1 && shared.indexOf("alpha") === -1, shared);

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
