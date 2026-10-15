// 139: checks for the symphony switcher's pure functions (scope, identity line,
// stored-choice validation, the header markup), run against the shipped
// symphonies.js with esc() from helpers.js in front of it.
//
// Usage: node symphony_switcher_checks.js <path-to-sliced-js>

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node symphony_switcher_checks.js <sliced-js>");
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
function checkTrue(name, value) { check(name, !!value, true); }

var _lastState = null;
var document;

eval(src);

// A two-symphony snapshot. P-cards belong to placeholder, C-cards to conductor-e2e.
function snapshot() {
  return {
    symphonies: [
      {
        name: "placeholder", github_project_number: 16, github_org: "ViviDynamics", repo: "placeholder-sample", cycle_count: 4,
        state: {
          board_snapshot: { TODO: ["P1"], BLOCKED: [], IN_PROGRESS: ["P2"], IN_REVIEW: [], DONE: ["P9"], BACKLOG: ["P8"] },
          board_titles: { P1: "Placeholder card" },
          active_sessions: { P2: {}, P3: {} },
        },
      },
      {
        name: "conductor-e2e", github_project_number: 3, github_org: "ViviDynamics", repo: "conductor-e2e-sample",
        state: {
          board_snapshot: { TODO: [], BLOCKED: ["C1"], IN_PROGRESS: [], IN_REVIEW: [], DONE: [], BACKLOG: [] },
          board_titles: {}, active_sessions: {},
        },
      },
    ],
    active_sessions: [
      { card_id: "P2", phase: "monitoring_performer" },
      { card_id: "P3", phase: "monitoring_performer" },
      { card_id: "C1", phase: "blocked" },
    ],
    open_questions: [
      { card_id: "C1", text: "Which DB?" },
      { card_id: "P1", text: "Which name?" },
      { text: "No card attached" },
    ],
    board_summary: { TODO: 9, IN_PROGRESS: 9, IN_REVIEW: 9, DONE: 9, BLOCKED: 9, BACKLOG: 9 },
    cycles_completed: 7,
    token_usage: {
      tracked: true, reason: null, day: "2026-10-14", week_start: "2026-10-08",
      today: { tokens: { total: 99 } }, week: { tokens: { total: 199 } },
      by_symphony: {
        placeholder: { today: { tokens: { total: 4 } }, week: { tokens: { total: 14 } } },
        "conductor-e2e": { today: { tokens: { total: 5 } }, week: { tokens: { total: 15 } } },
      },
    },
  };
}

// --- names and choices -------------------------------------------------------
check("names keep order and drop blanks and repeats", symphonyNames({
  symphonies: [{ name: "a" }, { name: "" }, { name: "b" }, { name: "a" }, null],
}), ["a", "b"]);
check("no symphonies means no names", symphonyNames({}), []);

check("a valid deep link wins", resolveSymphonyChoice("b", "a", ["a", "b"]), "b");
check("an unknown deep link falls back to the stored choice", resolveSymphonyChoice("zzz", "a", ["a", "b"]), "a");
check("an unknown stored choice falls back to All", resolveSymphonyChoice("", "gone", ["a", "b"]), "");
check("nothing chosen is All", resolveSymphonyChoice("", "", ["a", "b"]), "");
check("a removed symphony never hides everything", resolveSymphonyChoice("gone", "gone", ["a"]), "");

check("deep link read from a query", symphonyParamFromSearch("?symphony=conductor-e2e"), "conductor-e2e");
check("deep link among other parameters", symphonyParamFromSearch("?x=1&symphony=placeholder"), "placeholder");
check("no deep link", symphonyParamFromSearch("?x=1"), "");
check("empty query", symphonyParamFromSearch(""), "");
check("a malformed deep link is ignored", symphonyParamFromSearch("?symphony=%E0%A4%A"), "");
check("a bare parameter is ignored", symphonyParamFromSearch("?symphony"), "");

// --- card membership and scope -----------------------------------------------
const s = snapshot();
check("a card in a board belongs to that symphony", symphonyOfCard(s, "P1"), "placeholder");
check("a card only in the live sessions belongs to its symphony", symphonyOfCard(s, "P3"), "placeholder");
check("a card in the other board belongs to the other symphony", symphonyOfCard(s, "C1"), "conductor-e2e");
check("an unknown card belongs to none", symphonyOfCard(s, "NOPE"), "");
check("an empty card id belongs to none", symphonyOfCard(s, ""), "");

checkTrue("All keeps every card", symphonyKeepsCard(s, "", "C1"));
checkTrue("a choice keeps its own card", symphonyKeepsCard(s, "placeholder", "P1"));
check("a choice drops another symphony's card", symphonyKeepsCard(s, "placeholder", "C1"), false);
checkTrue("an item with no card stays under every choice", symphonyKeepsCard(s, "placeholder", ""));

const scoped = symphonyScope(s, "placeholder");
check("scope narrows the symphonies to the choice", scoped.symphonies.map(x => x.name), ["placeholder"]);
check("scope keeps only that symphony's sessions", scoped.active_sessions.map(x => x.card_id), ["P2", "P3"]);
check("scope keeps the questions of its cards and those with no card",
  scoped.open_questions.map(x => x.text), ["Which name?", "No card attached"]);
check("scope recounts the board for the one symphony", scoped.board_summary,
  { TODO: 1, IN_PROGRESS: 1, IN_REVIEW: 0, DONE: 1, BLOCKED: 0, BACKLOG: 1 });
check("scope shows that symphony's spend", scoped.token_usage.today, { tokens: { total: 4 } });
check("scope shows that symphony's weekly spend", scoped.token_usage.week, { tokens: { total: 14 } });
check("checks completed come from the chosen symphony, not the conductor's last check", scoped.cycles_completed, 4);
check("the snapshot itself is not changed", s.symphonies.length, 2);
check("All returns the snapshot unchanged", symphonyScope(s, "") === s, true);
check("a single symphony returns the snapshot unchanged",
  symphonyScope({ symphonies: [s.symphonies[0]], active_sessions: [{ card_id: "C1" }] }, "placeholder").active_sessions.length, 1);

// --- spend ---------------------------------------------------------------------
const noSpend = symphonySpend({ tracked: true, day: "d", week_start: "w", today: {}, week: {}, by_symphony: {} }, "ghost");
check("a symphony with no figures says so", [noSpend.tracked, typeof noSpend.reason], [false, "string"]);
check("a symphony with no figures shows no total", noSpend.today, null);
const untracked = { tracked: false, reason: "transcripts disabled" };
check("an untracked block passes through", symphonySpend(untracked, "placeholder"), untracked);
check("a missing block passes through", symphonySpend(undefined, "placeholder"), undefined);

// --- identity line ------------------------------------------------------------
const full = symphonyIdentity(s.symphonies[0]);
check("the identity line reads name, project and repository", full.text,
  "placeholder · GitHub Project #16 · ViviDynamics/placeholder-sample");
check("the project links to the org's project", full.projectUrl, "https://github.com/orgs/ViviDynamics/projects/16");
check("the repository links to GitHub", full.repoUrl, "https://github.com/ViviDynamics/placeholder-sample");
check("a name alone", symphonyIdentity({ name: "x" }).text, "x");
check("an unknown project number is left out, not printed",
  symphonyIdentity({ name: "x", github_project_number: null, github_org: "o", repo: "r" }).text, "x · o/r");
check("a zero project number is left out", symphonyIdentity({ name: "x", github_project_number: 0 }).text, "x");
check("a repository with no owner shows only the repository", symphonyIdentity({ name: "x", repo: "r" }).text, "x · r");
check("no owner means no GitHub link", symphonyIdentity({ name: "x", github_project_number: 2, repo: "r" }).projectUrl, "");
check("the identity line is unlinked when the owner is unknown",
  symphonyIdentityHtml({ name: "x", github_project_number: 2, repo: "r" }).includes("<a "), false);

check("only http and https links are kept", [safeHttpUrl("https://github.com/x"), safeHttpUrl("http://h/x")], ["https://github.com/x", "http://h/x"]);
check("a javascript link is dropped", safeHttpUrl("javascript:alert(1)"), "");
check("a data link is dropped", safeHttpUrl("data:text/html,x"), "");

const hostile = symphonyIdentityHtml({
  name: "<img src=x onerror=alert(1)>", github_project_number: 5, github_org: "o\"x", repo: "<b>r</b>",
});
checkTrue("a hostile name is escaped", hostile.includes("&lt;img src=x onerror=alert(1)&gt;"));
check("a hostile name is never markup", hostile.includes("<img"), false);
check("a hostile repository is escaped", hostile.includes("&lt;b&gt;r&lt;/b&gt;"), true);
// 156: an owner that is not a GitHub name is never linked, so it cannot reach an href at all.
check("a hostile owner is not linked, so it cannot break out of an href",
  symphonyIdentityHtml({ name: "x", github_project_number: 5, github_org: "o\"x", repo: "r" }).includes("href="), false);

// --- the header control ------------------------------------------------------
check("no symphonies, no control", symphonySwitcherHtml({}, ""), "");
const one = symphonySwitcherHtml({ symphonies: [s.symphonies[0]] }, "");
checkTrue("one symphony shows its identity", one.includes('id="symphony-identity"') && one.includes("GitHub Project #16"));
check("one symphony has no select", one.includes("<select"), false);

const many = symphonySwitcherHtml(s, "");
check("several symphonies get a select", many.includes('<select id="symphony-select"'), true);
check("All symphonies is first and selected", many.includes('<option value="" selected>All symphonies</option>'), true);
check("no identity line while All is chosen", many.includes('<span id="symphony-identity" class="muted"></span>'), true);
const chosen = symphonySwitcherHtml(s, "conductor-e2e");
check("the chosen symphony is selected", chosen.includes('<option value="conductor-e2e" selected>'), true);
check("the header names the chosen symphony's identity", chosen.replace(/<[^>]+>/g, "").includes("conductor-e2e · GitHub Project #3 · ViviDynamics/conductor-e2e-sample"), true);
check("the header does not say Board", /board/i.test(many + chosen + one), false);
const quoted = symphonySwitcherHtml({ symphonies: [{ name: "a\"b" }, { name: "c" }] }, "");
check("an option value is escaped", quoted.includes('<option value="a&quot;b">a&quot;b</option>'), true);

// --- counts --------------------------------------------------------------------
check("board summary sums the columns of the listed symphonies", symphonyBoardSummary(s.symphonies).TODO, 1);
check("board summary of nothing is zero", symphonyBoardSummary([]).BLOCKED, 0);

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
