// 233: checks for the card page's pure helpers, executed against the shipped cardpage.js.
// Covers the address a /cards path names, the link paths, the scope note and the live card's link.
//
// Usage: node card_page_233_checks.js <path-to-sliced-js>

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node card_page_233_checks.js <sliced-js>");
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
function has(name, haystack, needle) {
  check(name, String(haystack).indexOf(needle) !== -1, true);
}
function lacks(name, haystack, needle) {
  check(name, String(haystack).indexOf(needle) !== -1, false);
}

// The sliced file defines the page's helpers as globals. The document is never touched here.
globalThis.document = { getElementById: () => null };
globalThis.location = { pathname: "/", search: "" };
eval(src);

// --- which paths are card pages ------------------------------------------------- //

check("a card path is recognised", isCardPath("/cards/alpha-sym/PVTI_1"), true);
check("another path is not a card path", isCardPath("/history"), false);
check("a non-string is not a card path", isCardPath(null), false);

// --- the address a card path names ---------------------------------------------- //

check("a card path names its symphony and card", cardAddressFromPath("/cards/alpha-sym/PVTI_1"),
  { symphony: "alpha-sym", card: "PVTI_1" });
check("a trailing slash is allowed", cardAddressFromPath("/cards/alpha-sym/PVTI_1/"),
  { symphony: "alpha-sym", card: "PVTI_1" });
check("an encoded segment is decoded", cardAddressFromPath("/cards/alpha%2Dsym/PVTI_1"),
  { symphony: "alpha-sym", card: "PVTI_1" });
check("one segment is not an address", cardAddressFromPath("/cards/alpha-sym"), { invalid: true });
check("three segments are not an address", cardAddressFromPath("/cards/a/b/c"), { invalid: true });
check("a hostile card segment is invalid", cardAddressFromPath("/cards/alpha-sym/%3Cscript%3E"), { invalid: true });
check("a segment with a space is invalid", cardAddressFromPath("/cards/alpha-sym/a%20b"), { invalid: true });
check("a segment that does not decode is invalid", cardAddressFromPath("/cards/alpha-sym/%E0%A4%A"), { invalid: true });
check("a segment starting with a dot is invalid", cardAddressFromPath("/cards/alpha-sym/.hidden"), { invalid: true });
check("a leading underscore is invalid (the conductor folder is not a card page)",
  cardAddressFromPath("/cards/_conductor/PVTI_1"), { invalid: true });
check("a path that is not a card path is invalid", cardAddressFromPath("/history"), { invalid: true });

// --- the link paths -------------------------------------------------------------- //

check("a card path is built from encoded segments", cardPathFor("alpha-sym", "PVTI_1"), "/cards/alpha-sym/PVTI_1");
check("a card path encodes a segment that needs it", cardPathFor("a b", "c"), "/cards/a%20b/c");
check("a plausible address can be linked", cardPageAddressOk("alpha-sym", "PVTI_1"), true);
check("a symphony with a space cannot be linked", cardPageAddressOk("a b", "PVTI_1"), false);
check("a missing card cannot be linked", cardPageAddressOk("alpha-sym", ""), false);
check("a missing symphony cannot be linked", cardPageAddressOk(null, "PVTI_1"), false);

// --- the scope note --------------------------------------------------------------- //

check("no choice means no note", cardPageScopeNote("", "alpha-sym"), "");
check("the same symphony as the choice means no note", cardPageScopeNote("alpha-sym", "alpha-sym"), "");
check("no symphony means no note", cardPageScopeNote("alpha-sym", ""), "");
const note = cardPageScopeNote("alpha-sym", "beta-sym");
has("another symphony's card says so", note, "This card belongs to beta-sym.");
has("the note names the header's choice", note, "The header shows alpha-sym");
has("the note says how to see the card", note, "Choose All symphonies or beta-sym");
lacks("the note has no em dash", note, "\u2014");
lacks("the note has no en dash", note, "\u2013");

// --- the live card's link --------------------------------------------------------- //

const link = cardFullTimelineHtml("alpha-sym", "PVTI_1");
has("the full timeline link names the card page", link, 'href="/cards/alpha-sym/PVTI_1"');
has("the full timeline link navigates in the app", link, 'onclick="navigate(event,this.pathname)"');
has("the full timeline link has its label", link, ">Full timeline</a>");
check("a card with no address gets no link", cardFullTimelineHtml("", "PVTI_1"), "");
check("a hostile card id gets no link", cardFullTimelineHtml("alpha-sym", "<x>"), "");

// The address rule mirrors sanitize_segment: the same charset and length, and no edge dot or underscore.
const EDGE = [
  ["a/b", false], ["a b", false], ["abc.", false], ["-abc", true], ["-", true], ["a-", true],
  ["a..b", true], ["_abc", false], ["a_b_", false], ["x", true], ["1", true], ["a-b", true],
  ["ab_cd", true], ["unattributed", true], ["", false], [".", false], ["..", false],
  ["a\u00e9", false], ["a".repeat(120), true], ["a".repeat(121), false], ["a".repeat(119) + ".", false],
];
for (const [value, valid] of EDGE) {
  check("the segment rule for " + JSON.stringify(value.slice(0, 20)) + " (" + value.length + ")", cardSegmentOk(value), valid);
  const got = cardAddressFromPath("/cards/alpha-sym/" + encodeURIComponent(value));
  check("the route agrees for " + JSON.stringify(value.slice(0, 20)), got.invalid !== true, valid);
}
check("a symphony named unattributed gets no card page link", cardPageAddressOk("unattributed", "PVTI_1"), false);
check("a plausible symphony still gets one", cardPageAddressOk("alpha-sym", "PVTI_1"), true);

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
