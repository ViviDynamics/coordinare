// 139 review (second): every read of the full snapshot, _lastState, goes through the
// scoped view or is on SCOPE_UNSCOPED_READS in symphonies.js, with its reason.
//
// Usage: node last_state_scope_checks.js <symphonies.js> <dir>...
// A line that reads _lastState fails the check unless an allow-list entry for that
// file names it. An entry that matches no line is stale and fails too. Comment lines
// and the allow-list's own lines are not reads.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const [symphoniesPath, ...dirs] = process.argv.slice(2);
if (!symphoniesPath || dirs.length === 0) {
  console.error("usage: node last_state_scope_checks.js <symphonies.js> <dir>...");
  process.exit(1);
}

let passed = 0;
let failed = 0;
function check(name, ok, detail) {
  if (ok) { passed += 1; } else {
    failed += 1;
    console.error(`FAIL ${name}${detail ? `: ${detail}` : ""}`);
  }
}

const ctx = {};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(symphoniesPath, "utf8"), ctx);
const allow = vm.runInContext(
  "typeof SCOPE_UNSCOPED_READS !== 'undefined' ? SCOPE_UNSCOPED_READS : null", ctx,
);
check("the allow-list exists in symphonies.js", Array.isArray(allow), "SCOPE_UNSCOPED_READS is not declared");
const entries = Array.isArray(allow) ? [...allow] : [];

const matched = new Map(entries.map((entry) => [entry, 0]));
const reads = [];
for (const dir of dirs) {
  for (const name of fs.readdirSync(dir).filter((f) => f.endsWith(".js")).sort()) {
    const lines = fs.readFileSync(path.join(dir, name), "utf8").split("\n");
    lines.forEach((line, index) => {
      if (/^\s*\/\//.test(line) || /^\s*\{file:/.test(line)) return;
      if (!/\b_lastState\b/.test(line)) return;
      // A read that is passed straight into the scoped view is the rule, not an exception.
      if (/scopedView\(_lastState\)/.test(line)) return;
      const entry = entries.find((e) => e.file === name && line.includes(e.text));
      if (entry) {
        matched.set(entry, matched.get(entry) + 1);
      } else {
        reads.push(`${name}:${index + 1}: ${line.trim()}`);
      }
    });
  }
}

check("every read of _lastState goes through scopedView or is allow-listed", reads.length === 0,
  `${reads.length} unlisted read(s):\n  ${reads.join("\n  ")}`);
for (const entry of entries) {
  check(`the allow-list entry for ${entry.file} "${entry.text}" has a reason`,
    typeof entry.why === "string" && entry.why.trim().length > 0);
  check(`the allow-list entry for ${entry.file} "${entry.text}" still matches a read`,
    matched.get(entry) > 0, "stale entry");
}
check("the allow-list is short", entries.length <= 12, `${entries.length} entries`);

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
