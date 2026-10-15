// 150: checks for the History page's pure helpers, executed against the shipped history.js.
// Covers the query string, the collapsing of identical checks, the escaping of hostile server
// text, the links, the not-recorded readouts and the pager.
//
// Usage: node history_checks.js <path-to-sliced-js>

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node history_checks.js <sliced-js>");
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

// A stand-in page for the handlers that touch the document, the address and the network. The
// fetch stub never answers, so a request stays in flight until a check says otherwise.
const stubSection = { innerHTML: "", getAttribute: () => "1", setAttribute() {}, addEventListener() {} };
const fetchedUrls = [];
globalThis.document = { getElementById: (id) => (id === "history-page-section" ? stubSection : null) };
globalThis.history = { state: null, replaceState(state, title, url) { this.state = state; globalThis.location.search = url.replace(/^[^?]*/, ""); } };
globalThis.location = { pathname: "/history", search: "", hash: "" };
globalThis.fetch = (url) => { fetchedUrls.push(url); return new Promise(() => {}); };
globalThis.navigate = () => {};
globalThis.currentSymphonyChoice = () => "";
globalThis.symphonyNames = () => [];
globalThis.currentScopedState = () => null;
globalThis.SYMPHONY_ALL = "";

// The helpers touch no document until a page function runs, so the file evaluates as is.
eval(src);

function row(overrides) {
  return Object.assign({
    kind: "run", symphony: "coordinare", attributed_symphony: "coordinare", card: "PVTI_1", run: "run-1",
    started_at: "2026-10-01T10:00:00+00:00", stage: "implementing", stage_reason: null, decision: null,
    run_number: 2, run_number_reason: null, duration_seconds: 4, duration_reason: null,
    tokens: 1200, tokens_reason: null, cost_usd: 0.02, cost_reason: null,
    outcome: "completed", outcome_reason: null, outcome_text: null,
    pr_url: "https://github.com/o/r/pull/9", pr_reason: null, card_title: "Make history useful",
  }, overrides || {});
}

// --- the query string --------------------------------------------------------- //

check("an empty filter asks for the first page", historyQueryString(historyDefaultFilters(""), ""), "?limit=50");
check(
  "a symphony and a cursor are sent",
  historyQueryString({ symphony: "beta-sym", card: "", stage: "", outcome: "", since: "", until: "" }, "1|a|b|c"),
  "?symphony=beta-sym&cursor=1%7Ca%7Cb%7Cc&limit=50"
);
check(
  "a hostile filter value is encoded, not split into more parameters",
  historyQueryString({ symphony: "", card: "a&stage=x", stage: "", outcome: "", since: "", until: "" }, ""),
  "?card=a%26stage%3Dx&limit=50"
);
check("the default filters take the header's choice", historyDefaultFilters("alpha").symphony, "alpha");
check("the default filters with no choice are empty", historyDefaultFilters("").symphony, "");

// --- not recorded and links ---------------------------------------------------- //

has("a missing value says not recorded", historyNotRecorded("no usage"), "not recorded");
has("the reason is the tooltip, escaped", historyNotRecorded('a"<b>'), 'title="a&quot;&lt;b&gt;"');
has("a missing value carries its class", historyNotRecorded(null), 'class="history-missing"');
check("an https pull request link is kept", historySafeUrl("https://github.com/o/r/pull/9"), "https://github.com/o/r/pull/9");
check("a javascript link is dropped", historySafeUrl("javascript:alert(1)"), "");
check("a plain http link is dropped", historySafeUrl("http://example.com"), "");
check("a missing link is dropped", historySafeUrl(null), "");

// --- checks --------------------------------------------------------------------- //

check("a run is not a check", historyIsCheck(row({ kind: "run" })), false);
check("a conductor call is a check", historyIsCheck(row({ kind: "check" })), true);
check("no row is not a check", historyIsCheck(null), false);

const same = [
  row({ kind: "check", stage: "conductor:triage", outcome: "completed", decision: "proceed", duration_seconds: 2 }),
  row({ kind: "check", stage: "conductor:triage", outcome: "completed", decision: "proceed", duration_seconds: 4 }),
  row({ kind: "check", stage: "conductor:triage", outcome: "completed", decision: "proceed", duration_seconds: 6 }),
  row({ kind: "check", stage: "conductor:triage", outcome: "failed", decision: "proceed", duration_seconds: 1 }),
  row({ kind: "check", stage: "conductor:triage", outcome: "completed", decision: "wait", duration_seconds: 1 }),
];
const groups = historyCollapseChecks(same);
check("identical consecutive checks collapse into one group", groups.map(function(g) { return g.count; }), [3, 1, 1]);
check("a group keeps its rows newest first", groups[0].rows.length, 3);
check("a group's average is the mean of its durations", historyGroupAverage(groups[0]), 4);
check("a group's summary states its count and average", historyCheckSummary(groups[0]), "3 checks, all completed, avg 4.00s");
check("a single check's summary states its own result", historyCheckSummary(groups[1]), "Failed, 1.00s");
check("no checks collapse to no groups", historyCollapseChecks([]), []);
check("non-array input collapses to no groups", historyCollapseChecks(null), []);

const checksHtml = historyChecksHtml(same);
has("the checks section is collapsible", checksHtml, "<details");
has("the checks section counts its checks", checksHtml, "Checks (5)");
has("a collapsed group shows its count", checksHtml, "3 checks, all completed");
check("no checks render no section", historyChecksHtml([]), "");

// --- the run table -------------------------------------------------------------- //

const plain = historyRunRowHtml(row());
has("the transcript link is the viewer's address", plain, 'href="/runs/coordinare/PVTI_1/run-1"');
has("the transcript link is marked for the in-app router", plain, 'data-history-link="/runs/coordinare/PVTI_1/run-1"');
has("the outcome reads as completed", plain, "Completed");
has("the pull request is an external link", plain, 'href="https://github.com/o/r/pull/9"');
has("the cost is formatted", plain, "$0.02");
has("the tokens are formatted", plain, "1,200");

const hostile = historyRunRowHtml(row({
  card_title: '<img src=x onerror="window.__pwned = 1">',
  card: '"><script>window.__pwned = 2</script>',
  stage: "<b>implementing</b>",
  outcome_text: '"><img src=x>',
}));
lacks("a hostile title is not markup", hostile, "<img src=x onerror");
lacks("a hostile card id is not markup", hostile, "<script>");
lacks("a hostile stage is not markup", hostile, "<b>implementing");
has("a hostile title is shown as text", hostile, "&lt;img src=x onerror=");

const unknown = historyRunRowHtml(row({
  tokens: null, tokens_reason: "no usage records in this run", cost_usd: null, cost_reason: "no price configured",
  duration_seconds: null, duration_reason: "the run has no timestamps", run_number: null,
  run_number_reason: "the card has too many runs to number", pr_url: null, pr_reason: "no pull request link",
  stage: null, stage_reason: "the run has no header",
}));
has("missing tokens say not recorded with the reason", unknown, 'title="no usage records in this run"');
has("missing cost says not recorded with the reason", unknown, 'title="no price configured"');
has("missing duration says not recorded", unknown, "the run has no timestamps");
has("missing PR says not recorded", unknown, "no pull request link");
has("missing stage says not recorded", unknown, "the run has no header");

const weird = historyRunRowHtml(row({ outcome: "<x>" }));
lacks("an unknown outcome is not written as markup", weird, "<x>");
has("an unknown outcome is shown as not recorded", weird, "Not recorded");


check("an empty table says no runs match", historyTableHtml([]), '<p class="empty-state">No runs match these filters.</p>');
has("a table has a header for each column", historyTableHtml([row()]), "<th scope=\"col\">Tokens</th>");

// --- the form and the pager --------------------------------------------------- //

const form = historyFormHtml({ symphony: "beta-sym", card: '"x', stage: "", outcome: "failed", since: "", until: "" }, ["alpha", "beta-sym"]);
has("the form selects the current symphony", form, '<option value="beta-sym" selected>');
has("the form selects the current outcome", form, '<option value="failed" selected>');
has("the form escapes a card filter", form, 'value="&quot;x"');
has("the form offers a clear action", form, 'data-history-action="reset"');

has("an older page is offered when there is one", historyPagerHtml(false, true), 'data-history-action="older"');
check("an older page is not offered at the end", historyPagerHtml(false, false).indexOf('data-history-action="older" disabled') !== -1, true);
check("a newer page is not offered on the first page", historyPagerHtml(false, true).indexOf('data-history-action="newer" disabled') !== -1, true);

const page = historyPageHtml({ rows: [row(), row({ kind: "check", stage: "conductor:triage", decision: "proceed" })], next_cursor: "9|a|b|c", partial: true }, historyDefaultFilters(""), [], false);
has("the page holds the run table", page, "<table class=\"history-table\">");
has("the page holds the checks", page, "Checks (1)");
has("the page offers the next page", page, 'data-history-action="older"');
has("an incomplete listing is said", page, "This listing is incomplete.");

// --- review round: the filters live in the URL, and the notices -------------------- //

const fromUrl = historyFiltersFromSearch("?symphony=alpha&h.card=PVTI_1&h.outcome=failed", "alpha");
check("the filters are read from the URL", [fromUrl.card, fromUrl.outcome], ["PVTI_1", "failed"]);
check("a missing symphony filter takes the header's choice", fromUrl.symphony, "alpha");
const allChosen = historyFiltersFromSearch("?symphony=alpha&h.symphony=", "alpha");
check("an explicit All is kept in the URL", allChosen.symphony, "");
const rewritten = historySearchFor("?symphony=alpha&h.card=old&h.stage=qa", historyDefaultFilters("alpha"));
has("the header's symphony survives a filter change", rewritten, "symphony=alpha");
lacks("an old filter value is replaced, not kept", rewritten, "h.card=old");
lacks("an empty filter is left out of the URL", rewritten, "h.stage=qa");
has("the URL says the symphony filter explicitly", rewritten, "h.symphony=alpha");
check("a plain visit writes no History parameter", historySearchFor("", historyDefaultFilters("alpha"), "alpha"), "");
check("an explicit All under a scoped header is kept", historySearchFor("", historyDefaultFilters(""), "alpha"),
  "?h.symphony=");
check("All with no scope writes nothing", historySearchFor("", historyDefaultFilters(""), ""), "");
check("a saved stack for the same filters is kept", historyStackFor({ sig: "a", cursors: ["", "x"], index: 1 }, "a"),
  { cursors: ["", "x"], index: 1 });
check("a saved stack for other filters starts over", historyStackFor({ sig: "a", cursors: ["", "x"], index: 1 }, "b"),
  { cursors: [""], index: 0 });
check("no saved stack starts over", historyStackFor(null, "b"), { cursors: [""], index: 0 });

has("a newer run gives the refresh notice", historyNewerHtml({ newer_since_anchor: 2 }),
  "New runs since this list loaded: refresh");
check("no newer runs give no notice", historyNewerHtml({ newer_since_anchor: 0 }), "");
has("an incomplete listing says why", historyPartialHtml({ partial: true, partial_details: ["This listing is incomplete: x"] }),
  "This listing is incomplete: x");
check("a complete listing gives no incomplete notice", historyPartialHtml({ partial: false }), "");

// --- final review: the server's messages, refresh, in-flight paging, every reason ----- //

const badRequest = { innerHTML: "" };
historyShow(badRequest, { status: 400, body: { error: "bad_request", detail: "the card filter is not a card address" } });
has("a 400 shows the server's message", badRequest.innerHTML, "the card filter is not a card address");
const notFound = { innerHTML: "" };
historyShow(notFound, { status: 404, body: { error: "not_found", detail: "no symphony named x on this daemon" } });
has("a 404 shows the server's message", notFound.innerHTML, "no symphony named x on this daemon");
const unavailable = { innerHTML: "" };
historyShow(unavailable, { status: 200, body: { available: false, reason: "transcripts are not configured" } });
has("an unavailable listing shows its reason", unavailable.innerHTML, "transcripts are not configured");

_historyPage.cursors = ["", "CUR2"];
_historyPage.index = 1;
_historyPage.filters = historyDefaultFilters("");
_historyPage.scope = "";
_historyPage.body = { newer_since_anchor: 1, rows: [], next_cursor: null };
fetchedUrls.length = 0;
historyAction("refresh");
check("refresh loads page one, not the same cursor", fetchedUrls.length, 1);
lacks("refresh sends no cursor", fetchedUrls[0] || "", "cursor=");
check("refresh drops the stack", [_historyPage.cursors, _historyPage.index], [[""], 0]);

// The refresh above is still in flight. Its answer arrives first, as it would in a browser.
historyShow(stubSection, { status: 200, body: { available: true, rows: [], next_cursor: null } });
_historyPage.cursors = [""];
_historyPage.index = 0;
_historyPage.body = { rows: [], next_cursor: "N1", newer_since_anchor: 0 };
fetchedUrls.length = 0;
historyAction("older");
historyAction("older");
check("a second Older while one is in flight is ignored", _historyPage.cursors, ["", "N1"]);
check("only one request went out for two Older clicks", fetchedUrls.length, 1);
has("the pager is disabled while a request is in flight", historyPagerHtml(true, true, true), 'data-history-action="older" disabled');

const twoReasons = historyPartialHtml({ partial: true, partial_reasons: ["a", "b"],
  partial_details: ["This listing is incomplete: not every card was checked.", "Second reason here."] });
has("the first partial reason is shown", twoReasons, "not every card was checked");
has("the second partial reason is shown too", twoReasons, "Second reason here.");

// 233: the card cell links to the card page; a card id that is not a plausible name keeps the filter.
has("a card link goes to the card page", historyRunRowHtml(row()), 'data-history-card="/cards/coordinare/PVTI_1"');
has("the card page link is marked for the in-app router", historyRunRowHtml(row()), 'href="/cards/coordinare/PVTI_1"');
lacks("the card link no longer filters History", historyRunRowHtml(row()), "h.card=PVTI_1");
has("an implausible card id keeps the History filter", historyRunRowHtml(row({ card: "a b" })), "h.card=a");
lacks("an implausible card id gets no card page link", historyRunRowHtml(row({ card: "a b" })), "/cards/");
// 233: the unattributed folder is not a symphony's card page, so its card keeps the History filter.
has("an unattributed row keeps the History filter", historyRunRowHtml(row({ symphony: "unattributed" })), "h.card=PVTI_1");
lacks("an unattributed row gets no card page link", historyRunRowHtml(row({ symphony: "unattributed" })), "/cards/");
lacks("the run number column is gone", historyTableHtml([row()]), "Run (approx.)");
lacks("no run number is shown", historyRunRowHtml(row({ run_number: 3 })), "Run 3");

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
