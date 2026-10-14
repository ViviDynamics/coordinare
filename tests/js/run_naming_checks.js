// 141: checks for the run name helpers, the attempt wording and the copy action, executed against
// functions sliced out of the shipped helpers.js (and the glossary it reads).
//
// Usage: node run_naming_checks.js <path-to-sliced-js>

const fs = require("fs");
const path = require("path");

const jsPath = process.argv[2];
if (!jsPath) {
  console.error("usage: node run_naming_checks.js <sliced-js>");
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

// Browser stand-ins. The copy path reads navigator and document; the timers are captured so the
// checks run to completion without waiting for them.
let copied = null;
let execCalls = [];
let execResult = true;
let liveFields = 0;
const queuedTimers = [];
const fakeDocument = {
  body: {
    appendChild() { liveFields += 1; },
    removeChild() { liveFields -= 1; },
  },
  createElement() {
    return {
      value: "",
      style: {},
      setAttribute() {},
      select() { execCalls.push("select:" + this.value); },
    };
  },
  execCommand(command) { execCalls.push(command); return execResult; },
};
function setNavigator(mode) {
  const nav = mode === "none" ? {} : {
    clipboard: {
      writeText(text) {
        copied = text;
        return mode === "granted" ? Promise.resolve() : Promise.reject(new Error("permission denied"));
      },
    },
  };
  Object.defineProperty(globalThis, "navigator", { value: nav, configurable: true, writable: true });
}
Object.defineProperty(globalThis, "document", { value: fakeDocument, configurable: true, writable: true });
globalThis.setTimeout = function (fn) { queuedTimers.push(fn); return 0; };

eval(src);

function button(fullId) {
  return {
    textContent: "Copy",
    attrs: { "data-run-id": fullId },
    getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null; },
  };
}
function settle() { return new Promise((resolve) => setImmediate(resolve)); }

async function main() {
  // The short name.
  check("a UUID is named by its first six characters", runIdText("299a8349-6f0e-4b1e-9d2a-0c1f2e3d4a5b"), "run 299a83");
  check("a prefix ending in an underscore is not part of the name", runIdText("sess_abcdefgh"), "run abcdef");
  check("a missing id has no name", runIdText(null), "");

  // The attempt wording: only a known count is shown, and it names the stage.
  check("the second run of a stage", runNumberText("implementing", 2), "Run 2 of Implement");
  check("the first run of review", runNumberText("reviewing", 1), "Run 1 of Review");
  check("a QA count keeps the QA label", runNumberText("qa", 3), "Run 3 of QA");
  check("no count gives no wording", runNumberText("implementing", null), "");
  check("an absent count gives no wording", runNumberText("implementing", undefined), "");
  check("zero gives no wording", runNumberText("implementing", 0), "");
  check("a fraction gives no wording", runNumberText("implementing", 1.5), "");
  check("a negative count gives no wording", runNumberText("implementing", -2), "");
  check("a text count gives no wording", runNumberText("implementing", "two"), "");

  // The copy action with the clipboard granted.
  setNavigator("granted");
  let btn = button("299a8349-full-id");
  copyRunId(btn);
  await settle();
  check("the clipboard receives the full id", copied, "299a8349-full-id");
  check("the button reports the copy", btn.textContent, "Copied");

  // The clipboard refuses (Firefox without a grant): the id is selected and copied instead.
  setNavigator("denied");
  execCalls = [];
  execResult = true;
  btn = button("refused-full-id");
  copyRunId(btn);
  await settle();
  check("a refused clipboard falls back to the selected field", execCalls, ["select:refused-full-id", "copy"]);
  check("the fallback reports the copy", btn.textContent, "Copied");
  check("the temporary field is removed", liveFields, 0);

  // No clipboard API at all, and the copy command also fails: the button says so.
  setNavigator("none");
  execCalls = [];
  execResult = false;
  btn = button("no-api-full-id");
  copyRunId(btn);
  await settle();
  check("without the clipboard API the copy command is used", execCalls, ["select:no-api-full-id", "copy"]);
  check("a failed copy says so", btn.textContent, "Copy failed");

  // The label goes back to Copy after the pause.
  queuedTimers.splice(0).forEach((fn) => fn());
  check("the button returns to Copy", btn.textContent, "Copy");

  // A button without an id does nothing.
  execCalls = [];
  const empty = button("");
  copyRunId(empty);
  check("a button with no id copies nothing", execCalls, []);
  check("a button with no id keeps its label", empty.textContent, "Copy");
}

main().then(() => {
  console.log(`SUMMARY ${passed} ${failed}`);
  process.exit(failed === 0 ? 0 : 1);
});
