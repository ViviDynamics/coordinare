// 146: checks for the run detail's pure helpers (the address, the not-found
// decision and the run link), executed against the shipped functions.
//
// Usage: node run_detail_checks.js <path-to-sliced-js>

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node run_detail_checks.js <sliced-js>");
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
function has(name, haystack, needle) {
  check(name, String(haystack).indexOf(needle) !== -1, true);
}
function lacks(name, haystack, needle) {
  check(name, String(haystack).indexOf(needle) !== -1, false);
}

// The helpers touch no document, so the slice runs as is.
eval(src);

// --- the address ------------------------------------------------------------
check("the list path is a performers path", isPerformersPath("/performers"), true);
check("a role path is a performers path", isPerformersPath("/performers/implementing"), true);
check("a similar prefix is not", isPerformersPath("/performersx"), false);
check("the dashboard is not", isPerformersPath("/"), false);

check("the list has no role", performerRoleFromPath("/performers"), null);
check("a role is read from the path", performerRoleFromPath("/performers/implementing"), "implementing");
check("the segment is decoded", performerRoleFromPath("/performers/a%20b"), "a b");
check("a malformed segment decodes to no role", performerRoleFromPath("/performers/%E0%A4%A"), "");
check("a trailing slash is no role", performerRoleFromPath("/performers/"), "");

// --- the view ----------------------------------------------------------------
const ROLES = ["implementing", "reviewing"];
check("the list path shows the list", performerRouteView(null, ROLES), "list");
check("a known role shows its detail", performerRouteView("implementing", ROLES), "detail");
check("an unknown role is not found", performerRouteView("nope", ROLES), "not_found");
check("an empty role is not found", performerRouteView("", ROLES), "not_found");
check("a role with no roles configured is not found", performerRouteView("implementing", []), "not_found");
check("missing roles are not found, not a crash", performerRouteView("implementing", undefined), "not_found");
check("role names are compared exactly", performerRouteView("Implementing", ROLES), "not_found");
check("a prototype name is not a role", performerRouteView("constructor", ROLES), "not_found");

// --- the run link -------------------------------------------------------------
const link = viewRunLinkHtml({ performer_stage: "implementing" });
has("the link says View run", link, ">View run</a>");
has("the link goes to the run", link, 'href="/performers/implementing"');
has("a click navigates in place", link, "navigate(event,this.pathname)");
has("a click does not also open the card", link, "event.stopPropagation()");
has("a key does not also open the card", link, 'onkeydown="event.stopPropagation()"');
check("no stage means no link", viewRunLinkHtml({ card_id: "x" }), "");
check("no session means no link", viewRunLinkHtml(undefined), "");
const hostile = viewRunLinkHtml({ performer_stage: '"><script>alert(1)</script>' });
lacks("a hostile stage cannot open a tag", hostile, "<script");
lacks("a hostile stage cannot break the attribute", hostile, '"><');
has("a hostile stage is encoded in the address", hostile, "%3Cscript%3E");

const total = passed + failed;
console.log(`SUMMARY ${passed} ${failed}`);
console.log(`${total} checks run`);
process.exit(failed === 0 ? 0 : 1);
