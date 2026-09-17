# Copilot repository instructions

These instructions govern every Copilot code review in this repository. They exist to keep reviews small, cheap, and on-topic: the reviewer's job is to find real defects, not to grow the code.

## Review scope: code quality only

Report defects you can pin to an exact file:line with a concrete failure path:

- Correctness: logic errors, unhandled edge cases, broken error handling, resource and process lifecycle bugs, race conditions.
- Security: injection, unsafe subprocess or path handling, secrets exposure.
- Test integrity: missing coverage for changed behavior, assertions that do not pin behavior, tests that pass for the wrong reason.
- Contract mismatches: wire-schema disagreements between producer and consumer, or between a source of truth and its generated artifacts (report generation drift, not hand-edits).

## Off limits: code comments

This repository minimizes comments by policy. Existing comments are sparse on purpose and every one is deliberate.

- Never propose a finding whose resolution is adding, expanding, rewording, or removing a comment or docstring.
- Do not report existing comments as unclear, stale, or incomplete unless the comment contradicts the code's actual behavior (a factual error in the code, not in the prose).
- Do not ask for self-documenting names, type annotations, or explanatory prose where the code is correct.

## Off limits: churn

- No style preferences (naming taste, formatting, incidental abstractions) where behavior is correct.
- No refactors that change no behavior.
- No speculative findings ("this could be an issue if...") without a concrete failure path reachable from this codebase.

## Already-answered findings

Every review round in a PR is triaged in PR comments: accepted findings get fixes, rejected findings get evidence. Before raising a finding, read the PR's existing review threads.

- Do not re-raise a finding that was rejected with evidence in a prior round unless you have new evidence; cite the new evidence explicitly.
- Constants mirrored across the `coordinare` and `performer` packages are intentional (separate processes, no runtime imports) and are drift-guarded by CI tests. Do not ask to derive one from the other.
- Files under `specs/` and generated contract files are artifacts; request regeneration from source, not hand edits of generated output.
