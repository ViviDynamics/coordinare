// 136: checks for the glossary lookup, executed against the generated
// vocabulary.js plus the functions the Python test slices out of helpers.js.
//
// Usage: node vocabulary_checks.js <path-to-sliced-js>
//
// Deliberately NOT strict mode: the shipped block is loaded with a direct eval.

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node vocabulary_checks.js <sliced-js>");
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

var glossaryEl = { innerHTML: "" };
var document = { getElementById: function(id) { return id === "glossary-section" ? glossaryEl : null; } };

eval(src);

// Labels come from the glossary, for every kind of code.
check("conductor phase", humanPhase("monitoring_performer"), "Watching an agent");
check("check outcome", humanPhase("success"), "success");
check("the alias reads the same glossary", formatPhaseLabel("monitoring_pr"), "Waiting on review");

// Every label the page needs is in the data, not derived.
var labels = COORDINARE_VOCABULARY.labels;
check("every listed code has a non-empty label",
  Object.keys(labels).every(function(k) { return typeof labels[k] === "string" && labels[k].length > 0; }), true);

// An unknown code is shown as it is: no title-casing, no underscore rewriting.
check("unknown code is not prettified", humanPhase("brand_new_state"), "brand_new_state");
check("unknown code is still escaped", humanPhase("<img onerror=x>"), "&lt;img onerror=x&gt;");
check("empty and missing codes render empty", [humanPhase(""), humanPhase(null), humanPhase(undefined)], ["", "", ""]);
check("an inherited property name is not a label", humanPhase("constructor"), "constructor");
check("toString is not a label", humanPhase("toString"), "toString");

// Without the glossary script the lookup degrades to the raw code.
(function() {
  var saved = COORDINARE_VOCABULARY;
  COORDINARE_VOCABULARY = undefined;
  check("no glossary loaded shows the raw code", humanPhase("monitoring_pr"), "monitoring_pr");
  COORDINARE_VOCABULARY = saved;
})();

// The Glossary page explains the five nouns and lists the codes.
renderGlossaryPage();
["Symphony", "Stage", "Agent", "Run", "Check"].forEach(function(noun) {
  check("glossary explains " + noun, glossaryEl.innerHTML.indexOf("<strong>" + noun + "</strong>") !== -1, true);
});
check("glossary has a description for Run",
  glossaryEl.innerHTML.indexOf("One agent working on one stage of one card") !== -1, true);
check("glossary lists stages and conductor statuses",
  [glossaryEl.innerHTML.indexOf("<h3>Stages</h3>") !== -1, glossaryEl.innerHTML.indexOf("<h3>Conductor status</h3>") !== -1], [true, true]);

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
