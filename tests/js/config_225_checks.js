// 225: checks for the Settings page's required-secret Clear rule, the Show toggle's
// reveal rule, and the duplicate routing pairs warning. They run against the functions the
// Python test slices out of the shipped config.js, so they test what the browser runs.
//
// Usage: node config_225_checks.js <sliced-js> [<served-routing-json>]
// The optional JSON is {"entries": [...], "expected": [...]}: the entries the server's
// routing read returned, and the duplicate pairs the page must find in them.

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
const servedPath = process.argv[3];
if (!jsPath) {
  console.error("usage: node config_225_checks.js <sliced-js> [<served-json>]");
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

// --- Clear on a secret: only an optional one ---------------------------------
var required = cfgInput({key: "global.github_token", label: "GitHub token", type: "secret",
  secret: true, required: true, current_value: CFG_SECRET_MASK, editable: true});
has("a required secret offers no Clear", required, "clear-secret", false);
has("a required secret has no data-clear-for", required, "data-clear-for", false);
has("a required secret still has a Show toggle", required, 'data-cfg-action="toggle-secret"');

var optional = cfgInput({key: "global.dashboard_auth_token", label: "Dashboard auth token", type: "secret",
  secret: true, required: false, current_value: CFG_SECRET_MASK, editable: true});
has("an optional secret offers Clear", optional, 'data-cfg-action="clear-secret"');
has("the Clear names its field", optional, 'data-clear-for="dashboard_auth_token"');

var unset = cfgInput({key: "global.dashboard_auth_token", label: "Dashboard auth token", type: "secret",
  secret: true, current_value: null, editable: true});
has("a secret with no required key is treated as optional", unset, 'data-clear-for="dashboard_auth_token"');

// --- Show toggle --------------------------------------------------------------
has("a secret input is a password field", optional, 'type="password"');
has("the Show toggle starts not pressed", optional, 'aria-pressed="false"');
has("the Show toggle is a button", optional, '<button type="button"');
check("the mask may not be revealed", cfgMayReveal(CFG_SECRET_MASK), false);
check("typed text may be revealed", cfgMayReveal("ghp_typed"), true);
check("an empty field may be revealed", cfgMayReveal(""), true);
check("a mask-like value that is not the mask may be revealed", cfgMayReveal("••••••x"), true);

// --- duplicate routing pairs ---------------------------------------------------
function entry(backend, model) { return {backend: backend, model: model}; }
check("no entries, no duplicates", cfgDuplicatePairs([]), []);
check("null entries, no duplicates", cfgDuplicatePairs(null), []);
check("unique pairs, no duplicates",
  cfgDuplicatePairs([entry("vllm", "a"), entry("vllm", "b"), entry("ollama", "a")]), []);
check("one duplicate, both positions named",
  cfgDuplicatePairs([entry("vllm", "a"), entry("ollama", "b"), entry("vllm", "a")]),
  [{backend: "vllm", model: "a", positions: [1, 3]}]);
check("pairs in first-seen order, every position kept",
  cfgDuplicatePairs([entry("v", "a"), entry("o", "b"), entry("o", "b"), entry("v", "a"), entry("c", "x"), entry("v", "a")]),
  [{backend: "v", model: "a", positions: [1, 4, 6]}, {backend: "o", model: "b", positions: [2, 3]}]);
check("the same model on another backend is not a duplicate",
  cfgDuplicatePairs([entry("vllm", "a"), entry("ollama", "a")]), []);
check("the pair is the (backend, model) pair, not its reverse",
  cfgDuplicatePairs([entry("a", "b"), entry("b", "a")]), []);
check("a separator inside a name cannot make two pairs look alike",
  cfgDuplicatePairs([entry("a|b", "c"), entry("a", "b|c")]), []);
check("a missing backend and an empty one are the same pair",
  cfgDuplicatePairs([{model: "m"}, entry("", "m")]), [{backend: "", model: "m", positions: [1, 2]}]);
check("a numeric model and its string are the same pair",
  cfgDuplicatePairs([entry("v", 7), entry("v", "7")]), [{backend: "v", model: "7", positions: [1, 2]}]);
check("a backend named __proto__ is an ordinary pair",
  cfgDuplicatePairs([entry("__proto__", "x"), entry("__proto__", "x")]),
  [{backend: "__proto__", model: "x", positions: [1, 2]}]);

// --- the warning ---------------------------------------------------------------
check("no warning without duplicates", cfgDuplicateNotice([]), "");
check("no warning for null", cfgDuplicateNotice(null), "");
var notice = cfgDuplicateNotice([{backend: "vllm", model: "a", positions: [1, 3]}]);
has("the warning is marked for the page", notice, "data-routing-duplicates");
has("the warning is a status region", notice, 'role="status"');
has("the warning names the positions", notice, "entries 1, 3");
has("the warning names the backend", notice, "vllm");
has("the warning names the model", notice, "a");
has("the warning suggests removing one", notice, "Delete");
var hostile = cfgDuplicateNotice([{backend: "<img src=x onerror=alert(1)>", model: "m\"'", positions: [1, 2]}]);
has("a hostile backend is escaped", hostile, "&lt;img src=x onerror=alert(1)&gt;");
has("a hostile backend is not markup", hostile, "<img", false);
has("a hostile model is escaped", hostile, "m&quot;&#39;");
check("the warning uses no em or en dash", /[\u2013\u2014]/.test(notice + hostile), false);

// --- the server's own routing read ---------------------------------------------
if (servedPath) {
  const served = JSON.parse(fs.readFileSync(path.resolve(servedPath), "utf8"));
  check("the page finds the duplicates the server's read holds",
    cfgDuplicatePairs(served.entries), served.expected);
}

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
