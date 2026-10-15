// 123, 124, 125, 128: checks for the pure dashboard helpers, executed against
// the functions the Python test slices out of the shipped JS.
//
// Usage: node dashboard_ui_bug_checks.js <path-to-sliced-js>
//
// Deliberately NOT strict mode: the shipped block is loaded with a direct eval.

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node dashboard_ui_bug_checks.js <sliced-js>");
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
function has(name, haystack, needle, expected) {
  check(name, String(haystack).indexOf(needle) !== -1, expected !== false);
}

eval(src);

// --- 123: a busy role is not an error ---------------------------------------
check("1/1 busy, nothing queued, uses the normal colour",
  utilizationBarColor({ role: "implementer", active: 1, max: 1, queued: 0 }), "var(--color-bar-fill)");
check("missing queued field uses the normal colour",
  utilizationBarColor({ role: "implementer", active: 1, max: 1 }), "var(--color-bar-fill)");
check("queued work is highlighted",
  utilizationBarColor({ role: "implementer", active: 1, max: 1, queued: 2 }), "var(--color-accent-yellow)");
// 154: the bar moved to the Performers page table (utilizationBarHtml); the busy text is its own cell.
var busyBar = utilizationBarHtml({ role: "implementer", active: 1, max: 1, queued: 0 });
has("busy bar never uses the full/red colour", busyBar, "color-bar-full", false);
has("busy bar is the normal fill", busyBar, "var(--color-bar-fill)");
var queuedBar = utilizationBarHtml({ role: "qa", active: 1, max: 1, queued: 3 });
has("queued bar is highlighted", queuedBar, "var(--color-accent-yellow)");
has("the bar is 100 percent full when the role is full", busyBar, "width:100%");

// --- 124: no raw ProjectV2 item id in the visible text -----------------------
const NODE = "PVTI_lADOBjmjsc4Bkx5YzhANaH0";
function visibleText(html) { return html.replace(/<[^>]*>/g, ""); }
var ref = cardRefHtml({ card_id: NODE, issue_number: 57, issue_url: "https://github.com/acme/repo/issues/57" });
has("issue number is shown", visibleText(ref), "#57");
has("node id is not in the visible text", visibleText(ref), "PVTI_", false);
has("node id stays available on hover", ref, 'title="Card ' + NODE + '"');
has("issue number links to the issue", ref, 'href="https://github.com/acme/repo/issues/57"');
var withPr = cardRefHtml({ card_id: NODE, issue_number: 57, issue_url: "https://github.com/acme/repo/issues/57",
  pr_url: "https://github.com/acme/repo/pull/81" });
has("PR chip is shown when there is a PR", visibleText(withPr), "PR #81");
has("PR chip links to the PR", withPr, 'href="https://github.com/acme/repo/pull/81"');
var bare = cardRefHtml({ card_id: NODE });
has("no issue number still hides the node id", visibleText(bare), "PVTI_", false);
check("no issue number falls back to a plain label", visibleText(bare), "Card");
has("a javascript: issue url is never linked",
  cardRefHtml({ card_id: NODE, issue_number: 1, issue_url: "javascript:alert(1)" }), "href", false);
has("a javascript: pr url produces no PR chip",
  cardRefHtml({ card_id: NODE, issue_number: 1, pr_url: "javascript:alert(1)" }), "PR", false);
check("missing session does not throw", visibleText(cardRefHtml(undefined)), "Card");

// --- 125: readouts carry their start point -----------------------------------
var since = "2026-10-13T12:00:00Z";
var el = elapsedSinceHtml(since);
has("elapsed readout is a relative time from its start point", el, 'data-rel-iso="' + since + '"');
has("elapsed readout shows the exact time on hover", el, 'title="');
check("no start point says unknown, never a dash (155)", elapsedSinceHtml(null), "<strong>unknown</strong>");
// Two readouts for the same run, refreshed by the one timer, agree.
var a = { attrs: { "data-rel-iso": since }, textContent: "stale-a",
  getAttribute(k) { return this.attrs[k] || null; } };
var b = { attrs: { "data-rel-iso": since }, textContent: "stale-b",
  getAttribute(k) { return this.attrs[k] || null; } };
refreshRelativeTimes({ querySelectorAll() { return [a, b]; } });
check("two readouts for one run show the same age", b.textContent, a.textContent);
check("readout was refreshed from its own start point", a.textContent, fmtAgo(since));

// --- 128: identical consecutive checks collapse ------------------------------
function row(phase, outcome, duration, ts) { return { phase: phase, outcome: outcome, duration_seconds: duration, timestamp: ts }; }
var wall = [];
for (var i = 0; i < 12; i++) wall.push(row("monitoring_performer", "success", 5 + (i % 2), "t" + i));
var collapsed = collapseCycleRows(wall);
check("twelve identical checks are one row", collapsed.length, 1);
check("the row counts them", collapsed[0].count, 12);
check("the row averages their duration", collapsed[0].duration_seconds, 5.5);
check("the row keeps the newest timestamp first seen", collapsed[0].timestamp, "t0");
var mixed = collapseCycleRows([
  row("monitoring_performer", "success", 5, "a"), row("monitoring_performer", "success", 7, "b"),
  row("monitoring_performer", "error", 1, "c"),
  row("dispatching", "success", 2, "d"),
  row("monitoring_performer", "success", 4, "e"),
]);
check("a different outcome or phase starts a new row", mixed.map(function(r) { return r.count; }), [2, 1, 1, 1]);
check("only consecutive rows merge", mixed.length, 4);
check("missing outcome counts as success",
  collapseCycleRows([{ phase: "idle" }, { phase: "idle", outcome: "success" }]).length, 1);
check("a row with no durations has no average",
  collapseCycleRows([{ phase: "idle" }, { phase: "idle" }])[0].duration_seconds, null);
check("empty and missing history yield no rows", [collapseCycleRows([]).length, collapseCycleRows(null).length], [0, 0]);

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
