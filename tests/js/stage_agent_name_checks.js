// 137: checks for the stage and agent display names, executed against the
// generated vocabulary.js plus the functions sliced out of helpers.js.
//
// Usage: node stage_agent_name_checks.js <path-to-sliced-js>

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node stage_agent_name_checks.js <sliced-js>");
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

eval(src);

// One name per stage and per agent.
check("stage: implementing", stageLabel("implementing"), "Implement");
check("stage: architecting", stageLabel("architecting"), "Plan");
check("stage: documenting", stageLabel("documenting"), "Docs");
check("stage: closing_review", stageLabel("closing_review"), "Close");
check("stage: qa is upper-cased", stageLabel("qa"), "QA");
check("agent: tech_writer", agentLabel("tech_writer"), "Documenter");
check("agent: closer", agentLabel("closer"), "Closer");
check("agent: curator has no stage and says what it does", agentLabel("curator"), "Intake (issue triage)");
check("agent: advocate", agentLabel("advocate"), "Issue responder");

// The same code, two kinds, two names.
check("security as a stage", stageLabel("security"), "Security");
check("security as an agent", agentLabel("security"), "Security reviewer");
check("the phase lookup does not answer for stages", humanPhase("implementing"), "implementing");

// Always shown as a pair.
check("pair: implementing", stageAgentLabel("implementing"), "Implement · Implementer");
check("pair: documenting", stageAgentLabel("documenting"), "Docs · Documenter");
check("pair: security", stageAgentLabel("security"), "Security · Security reviewer");
check("unknown stage is shown as it is", stageAgentLabel("translating"), "translating");
check("unknown stage is escaped", stageAgentLabel("<b>"), "&lt;b&gt;");
check("empty stage renders empty", stageAgentLabel(""), "");

// A role written either way resolves to the same pair.
check("role given as a stage code", roleOrStageLabel("implementing"), "Implement · Implementer");
check("role given as an agent code", roleOrStageLabel("implementer"), "Implement · Implementer");
check("agent with no stage shows its own name", roleOrStageLabel("curator"), "Intake (issue triage)");
check("unknown role is shown as it is", roleOrStageLabel("translator"), "translator");

// Which agent does a stage.
check("agent for a stage", agentForStage("closing_review"), "closer");
check("no agent for an unknown stage", agentForStage("nope"), "");
check("inherited property is not a stage", agentForStage("constructor"), "");

// Personas page: which stage each agent serves.
check("persona with a stage", personaStageNote("tech_writer").indexOf("· Docs stage") !== -1, true);
check("persona without a stage", personaStageNote("curator").indexOf("· no pipeline stage") !== -1, true);

console.log(`SUMMARY ${passed} ${failed}`);
process.exit(failed === 0 ? 0 : 1);
