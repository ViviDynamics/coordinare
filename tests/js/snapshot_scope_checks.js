// 139 review, item 1: every top-level snapshot field has a declared scope, and a
// selected symphony's view derives its per-card and per-cycle values from that
// symphony's own entry or its own sessions.
//
// Usage: node snapshot_scope_checks.js <symphonies.js> <server-keys.json>
// server-keys.json lists the top-level keys the server's snapshot builder emits.

const fs = require("fs");
const vm = require("vm");

const [jsPath, keysPath] = process.argv.slice(2);
if (!jsPath || !keysPath) {
  console.error("usage: node snapshot_scope_checks.js <symphonies.js> <server-keys.json>");
  process.exit(1);
}

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

// The scope module reads only its own functions, so it runs in a bare context.
const ctx = {
  document: { getElementById: () => null },
  console,
  location: { search: "", pathname: "/" },
  window: { localStorage: null },
};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(jsPath, "utf8"), ctx);
const SCOPE = vm.runInContext("SNAPSHOT_FIELD_SCOPE", ctx);
const KINDS = ["scoped", "derived", "shared", "hidden"];
const serverKeys = JSON.parse(fs.readFileSync(keysPath, "utf8"));

// --- the table covers what the server sends ---------------------------------- #
const missing = serverKeys.filter((key) => !Object.prototype.hasOwnProperty.call(SCOPE, key));
check("every server key has a declared scope", missing, []);
checkTrue("the server key list is not empty", serverKeys.length > 30);
const badKinds = Object.keys(SCOPE).filter((key) => !KINDS.includes(SCOPE[key]));
check("every scope is one of the four kinds", badKinds, []);

// --- a selected symphony's view ----------------------------------------------- #
function symphony(name, number, extra) {
  return Object.assign({
    name, github_project_number: number, github_org: "acme", repo: `${name}-repo`,
    cycle_count: 0, error_count: 0, last_poll_at: null, poll_interval_seconds: 30,
    ownership_hint: `${name} hint`,
    state: {
      board_snapshot: { TODO: [], BLOCKED: [], IN_PROGRESS: [], IN_REVIEW: [], DONE: [], BACKLOG: [] },
      board_titles: {}, active_sessions: {}, session_skip_reasons: {},
    },
  }, extra || {});
}
const alphaCard = { card_id: "A1", card_title: "Alpha card", phase: "monitoring_pr", performer_stage: "reviewing",
  session_id: "sess-alpha", agent_dispatch_at: "2026-10-14T09:00:00+00:00", card_tokens_total: 4242,
  card_cost_estimate: 1.5, issue_url: null, pr_url: null, performer_events: [], performer_metrics: null,
  performer_logs: [], performer_backend: "claude" };
function snapshot() {
  const alpha = symphony("alpha", 16, { cycle_count: 7, error_count: 1, last_poll_at: "2026-10-14T09:59:00+00:00",
    poll_interval_seconds: 45, ownership_hint: "alpha hint",
    state: { board_snapshot: { TODO: ["A1"], BLOCKED: [], IN_PROGRESS: [], IN_REVIEW: [], DONE: [], BACKLOG: [] },
      board_titles: { A1: "Alpha card" }, active_sessions: { A1: {} }, session_skip_reasons: { A1: { reason: "queued" } } } });
  const beta = symphony("beta", 2, { cycle_count: 2, error_count: 0, last_poll_at: "2026-10-14T09:58:00+00:00",
    poll_interval_seconds: 30, ownership_hint: "beta hint" });
  return {
    symphonies: [alpha, beta],
    active_sessions: [alphaCard, { card_id: "X9", card_title: "Unclaimed card", phase: "blocked", performer_stage: "" }],
    open_questions: [{ card_id: "X9", text: "Who owns this?" }, { card_id: "A1", text: "Alpha question" }],
    phase: "monitoring_pr", phase_label: "Waiting on review", agent_dispatch_at: "2026-10-14T09:00:00+00:00",
    card_tokens_total: 4242, agent_session_id: "sess-alpha", pr_url: "https://github.com/acme/x/pull/1",
    cycles_completed: 7, consecutive_error_count: 1, last_cycle_duration_seconds: 12.5,
    cycle_history: [{ cycle: 1 }], blocked_by_dependencies: [{ issue_number: 5 }], last_rebase_round: { x: 1 },
    backend_ui_url: "http://alpha.local", session_stats: { calls: 3 }, subsystems: [{ name: "github" }],
    role_utilization: [{ role: "implementer" }], daemon_running: true,
    board_summary: { TODO: 1, BLOCKED: 0, IN_PROGRESS: 0, IN_REVIEW: 0, DONE: 0, BACKLOG: 0 },
    token_usage: { tracked: true, reason: null, day: "d", week_start: "w", today: { t: 1 }, week: { t: 2 },
      by_symphony: { alpha: { today: { t: "a" }, week: { t: "aw" } }, beta: { today: { t: "b" }, week: { t: "bw" } } } },
    session_skip_reasons: {},
  };
}

const beta = vm.runInContext("symphonyScope", ctx)(snapshot(), "beta");
check("a selected symphony's phase is its own, idle when it has no session", beta.phase, "idle");
check("the headline has no label of its own, so it is worded from the phase", beta.phase_label, null);
check("card tokens are the selected symphony's own, zero without a session", beta.card_tokens_total, 0);
check("the agent session belongs to the selected symphony's session only", beta.agent_session_id, null);
check("no pull request is carried from another symphony", beta.pr_url, null);
check("checks completed are the selected symphony's", beta.cycles_completed, 2);
check("consecutive errors are the selected symphony's", beta.consecutive_error_count, 0);
check("the poll interval is the selected symphony's", beta.poll_interval_seconds, 30);
check("the last check time is the selected symphony's", beta.last_poll_at, "2026-10-14T09:58:00+00:00");
check("the ownership hint is the selected symphony's", beta.ownership_hint, "beta hint");
check("the spend is the selected symphony's", beta.token_usage.today, { t: "b" });
check("the last check's duration is not known per symphony, so it is hidden", beta.last_cycle_duration_seconds, null);
check("the recent checks are hidden under a symphony", beta.cycle_history, []);
check("the dependency blockers are hidden under a symphony", beta.blocked_by_dependencies, []);
check("the rebase round is hidden under a symphony", beta.last_rebase_round, null);
check("the backend link is hidden under a symphony", beta.backend_ui_url, null);
check("the session stats are hidden under a symphony", beta.session_stats, null);
check("deployment-wide values stay, labelled as shared", beta.subsystems, [{ name: "github" }]);
check("the capacity table stays, labelled as shared", beta.role_utilization, [{ role: "implementer" }]);
check("the scope names what it hid", [...beta.scope_hidden].sort(), [
  "backend_ui_url", "blocked_by_dependencies", "cycle_history", "last_cycle_duration_seconds",
  "last_rebase_round", "session_stats",
]);

// An unclaimed card's sessions and questions stay visible under a symphony, and say so.
const unclaimedSession = beta.active_sessions.find((sess) => sess.card_id === "X9");
checkTrue("a session on an unclaimed card stays under a symphony", unclaimedSession);
check("an unclaimed session is marked as not on a symphony's board", unclaimedSession && unclaimedSession.unclaimed, true);
check("an unclaimed question stays under a symphony, marked",
  beta.open_questions.filter((q) => q.card_id === "X9").map((q) => q.unclaimed), [true]);
check("another symphony's question is dropped",
  beta.open_questions.some((q) => q.card_id === "A1"), false);

// All symphonies is the snapshot as it arrived.
const full = snapshot();
check("All is the snapshot unchanged", vm.runInContext("symphonyScope", ctx)(full, "") === full, true);

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
