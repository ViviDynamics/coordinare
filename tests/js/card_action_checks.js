// 145: checks for the card action menu's pure helpers, executed against the
// shipped actions.js plus the label helpers it uses.
//
// Usage: node card_action_checks.js <path-to-sliced-js>

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node card_action_checks.js <sliced-js>");
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

// actions.js registers a click listener when a document exists; none here.
eval(src);

const NODE = "PVTI_lADOBjmjsc4Bkx5YzhANaH0";
const state = { lifecycle_sequence: ["assessing", "implementing", "reviewing", "qa"] };

// --- each action states its effect before it runs ----------------------------
has("cancel says what it does", cardActionEffect("cancel"), "stops the agent");
has("cancel says where the card goes", cardActionEffect("cancel"), "back to To Do");
has("skip says what it does", cardActionEffect("skip"), "moves on to its next stage");
has("restart names the stage by its display name", cardActionEffect("restart", "implementing"), "Restart from Implement?");
has("restart says later stages run again", cardActionEffect("restart", "implementing"), "every later stage runs again");
check("an unknown action has no effect text", cardActionEffect("explode"), "");

// --- each action hits the existing endpoint, scoped to the card --------------
check("cancel request", cardActionRequest("cancel", NODE),
  { method: "POST", url: "/api/cancel?card_id=" + NODE });
check("skip request", cardActionRequest("skip", NODE),
  { method: "POST", url: "/api/skip-role?card_id=" + NODE });
check("restart request", cardActionRequest("restart", NODE, "implementing"),
  { method: "POST", url: "/api/restart-from/implementing?card_id=" + NODE });
check("card ids and stages are URL-encoded",
  cardActionRequest("restart", "a b&c", "x/y").url, "/api/restart-from/x%2Fy?card_id=a%20b%26c");
check("unknown action sends nothing", cardActionRequest("explode", NODE), null);

// --- responses in plain language ---------------------------------------------
check("409 during a check offers a retry",
  cardActionOutcome("cancel", 409, { status: "cycle_in_progress", error: "A cycle is in progress" }),
  { text: "A check is in progress. Try again in a moment.", retry: true });
check("cancel refused for a card that is not the active one",
  cardActionOutcome("cancel", 409, { status: "not_active_card" }).retry, false);
has("and it says nothing was cancelled",
  cardActionOutcome("cancel", 409, { status: "not_active_card" }).text, "Nothing was cancelled");
check("a pending control conflict shows the server's reason",
  cardActionOutcome("skip", 409, { error: "A control is already pending for this card" }).text,
  "A control is already pending for this card");
check("a refusal shows the server's reason",
  cardActionOutcome("restart", 400, { error: "Role 'x' not in lifecycle" }).text, "Role 'x' not in lifecycle");
check("cancelled", cardActionOutcome("cancel", 200, { status: "cancelled" }).text, "Card cancelled.");
check("nothing to cancel", cardActionOutcome("cancel", 200, { status: "no_active_card" }).text, "There was no active card to cancel.");
check("queued controls say when they apply",
  cardActionOutcome("skip", 200, { status: "override_queued" }).text, "Queued. It takes effect on the next check.");
check("a missing body does not throw", cardActionOutcome("skip", 500, undefined).text, "The request was refused.");

// --- the menu ----------------------------------------------------------------
var menu = cardActionsHtml({ card_id: NODE, phase: "monitoring_performer" }, state);
has("menu offers cancel", menu, 'data-card-action="cancel"');
has("menu offers skip", menu, 'data-card-action="skip"');
has("menu offers restart", menu, 'data-card-action="restart"');
has("every button carries the card id", menu.split('data-card-id="' + NODE + '"').length === 4, "true");
has("restart lists the lifecycle stages by display name", menu, '<option value="implementing">Implement</option>');
has("the menu has an accessible name", menu, 'aria-label="Card actions"');
has("the menu has a live status region", menu, 'role="status"');
check("no menu for a card that is not running",
  [cardActionsHtml({ card_id: NODE, phase: "monitoring_pr" }, state), cardActionsHtml({ card_id: NODE, phase: "blocked" }, state)], ["", ""]);
check("no menu without a card id", cardActionsHtml({ phase: "monitoring_performer" }, state), "");
check("no menu for a missing session", cardActionsHtml(undefined, state), "");
has("no restart control when the lifecycle is unknown",
  cardActionsHtml({ card_id: NODE, phase: "dispatching" }, {}), 'data-card-action="restart"', false);
has("card ids are escaped in the markup",
  cardActionsHtml({ card_id: '"><img>', phase: "dispatching" }, state), "<img>", false);

// --- pending chip ------------------------------------------------------------
check("pending skip", pendingControlText({ action: "skip" }), "Skip pending");
check("pending restart names the stage", pendingControlText({ action: "restart", target_stage: "reviewing" }), "Restart from Review pending");
check("nothing pending", [pendingControlText(null), pendingControlText({})], ["", ""]);
check("no chip when nothing is pending", pendingControlHtml({}), "");
has("chip explains itself", pendingControlHtml({ pending_control: { action: "skip" } }), "applied on the next check");

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
