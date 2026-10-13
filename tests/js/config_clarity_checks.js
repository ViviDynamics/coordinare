// 126: checks for the config page's plain-language status line and the
// per-field symphony override note, executed against the functions the
// Python test slices out of the shipped config.js.
//
// Usage: node config_clarity_checks.js <path-to-sliced-js>

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node config_clarity_checks.js <sliced-js>");
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

var CFG_HINT = "font-size:11px";
var _cfgOverrides = {};

eval(src);

// --- status line -------------------------------------------------------------
var fresh = cfgStatusLine({ config_version: 0, routing_available: false, routing_mounted: false });
has("version 0 is explained, not shown as a number", fresh, "Loaded when the daemon started; not reloaded since.");
has("the bare phrase 'Config version' is gone", fresh, "Config version", false);
has("unmounted routing is explained", fresh, "No routing table is mounted on this host, so model routing uses defaults.");
has("the bare phrase 'routing unavailable' is gone", fresh, "routing unavailable", false);
has("one reload reads naturally",
  cfgStatusLine({ config_version: 1, routing_available: true }), "Reloaded 1 time since the daemon started.");
has("several reloads read naturally",
  cfgStatusLine({ config_version: 4, routing_available: true }), "Reloaded 4 times since the daemon started.");
has("an available routing table says where to edit it",
  cfgStatusLine({ config_version: 0, routing_available: true }), "can be edited under Routing");
has("a mounted but missing routing file is told apart from an unmounted one",
  cfgStatusLine({ config_version: 0, routing_available: false, routing_mounted: true }), "its file is missing on this host");
has("a missing version does not print undefined", cfgStatusLine({}), "undefined", false);

// --- override note -----------------------------------------------------------
check("a field nobody overrides has no note", cfgOverrideNote("max_feedback_cycles"), "");
_cfgOverrides = {
  github_project_number: [{ symphony: "placeholder", value: "#5" }, { symphony: "conductor-e2e", value: "#16" }],
  project_name: [{ symphony: "conductor-e2e", value: null }],
  odd: [{ symphony: "a b/<c>", value: null }],
};
var note = cfgOverrideNote("github_project_number");
has("the note names each overriding symphony", note, ">placeholder</a> (#5)");
has("the note shows the value in effect for each", note, ">conductor-e2e</a> (#16)");
has("each symphony links to its page", note, 'href="/symphonies/conductor-e2e"');
has("plural wording for several symphonies", note, "Overridden by symphonies ");
has("the note says what the global value still applies to", note, "applies only to a symphony that does not set its own");
var single = cfgOverrideNote("project_name");
has("singular wording for one symphony", single, "Overridden by symphony ");
has("an override with no shown value prints no brackets", single, "</a> (", false);
var odd = cfgOverrideNote("odd");
has("symphony names are URL-encoded in the link", odd, 'href="/symphonies/a%20b%2F%3Cc%3E"');
has("symphony names are HTML-escaped in the text", odd, "a b/&lt;c&gt;");
check("an inherited property name is not an override list", cfgOverrideNote("constructor"), "");
_cfgOverrides = null;
check("missing override data renders nothing", cfgOverrideNote("github_project_number"), "");

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
