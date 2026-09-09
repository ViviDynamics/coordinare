# Live harness comparison — 2026-09-09

Keep the current role assignments provisional. This eight-point live sweep does not
support replacing them: the nonbaseline candidates did not reach the existing
three-conclusive-dispatch evidence floor. The per-role result is advisory, and no
live deployment configuration was changed.

## Method and provenance

The original `benchmarks/spaces/harness.yaml` dimensions were used: reviewer
(OpenClaw, Claude Code, Codex, Junie), security (OpenClaw, Claude Code, Codex), and
implementer (OpenCode, Codex, Claude Code). The all-Claude baseline plus seven
single-role substitutions were run once each against `tiny-multiply`, with a
600-second wall-clock budget per point. The model stayed `spark/glm-5.3-flash`,
served exclusively through `https://litellm.vividynamics.com`. Model hosts were
not probed directly. Roles outside those dimensions stayed Claude Code.

These are real ephemeral performer containers, CLI tools, Git workspaces and model
calls, against the spec151 simulated GitHub/board boundary and real pytest acceptance
gate. They are not production board issues. The performer image was
`coordinare-performer:full`, ID
`sha256:caabbf22e60f8e0682cc28bf2a1926af8321a464bf2d901e0726466944c2f4ca`.
Claude Code reported version 2.1.266. Vendor CLIs are installed at startup, so the
image ID alone does not freeze every CLI version. The Switchyard evaluation shared
the gateway during part of the sweep; timings cannot establish isolated throughput
or hardware headroom.

A completed Claude baseline was retained unchanged from the initial sweep. Its
unused `CODEX_PROVIDER_WIRE_API=chat` environment entry was removed before all
seven final substitution cells because the installed Codex accepts only the
Responses wire protocol. No actual Codex sample in the retained evidence used the
invalid setting. The baseline's original configuration fingerprint is preserved,
so it truthfully differs in that inactive environment detail. Interrupted earlier
substitution cells and all preparation pilots are excluded from the ranking.

## Dispatch evidence

Counts are pooled across the eight cells, with duplicate session registrations
collapsed by the existing rollup. Positive means a successful terminal marker;
legitimate negative includes `changes_requested`, `qa_failed`, `security_failed`
and `blocked`. Both earn harness credit because producing a structured negative
verdict is a working harness. These negatives do not establish that the model was
wrong, and positive stage completion does not prove final fixture correctness.
Conclusive means credit plus explicit harness defect. Rates exclude environment
and inconclusive observations. Missing evidence is unknown, never a measured zero
rate.

| Role | Harness | Dispatches | Conclusive | Positive | Legitimate negative | Harness defect | Environment | Inconclusive | Credit / defect rate |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| reviewing | claude_code | 5 | 2 | 1 | 1 | 0 | 0 | 3 | 100% / 0% |
| reviewing | codex | 1 | 1 | 0 | 1 | 0 | 0 | 0 | 100% / 0% |
| reviewing | junie | 1 | 0 | 0 | 0 | 0 | 0 | 1 | unknown / unknown |
| reviewing | openclaw | 1 | 0 | 0 | 0 | 0 | 0 | 1 | unknown / unknown |
| security | claude_code (not observed) | 0 | 0 | 0 | 0 | 0 | 0 | 0 | unknown / unknown |
| security | codex (not observed) | 0 | 0 | 0 | 0 | 0 | 0 | 0 | unknown / unknown |
| security | openclaw | 1 | 0 | 0 | 0 | 0 | 0 | 1 | unknown / unknown |
| implementing | claude_code | 6 | 6 | 6 | 0 | 0 | 0 | 0 | 100% / 0% |
| implementing | codex | 1 | 1 | 1 | 0 | 0 | 0 | 0 | 100% / 0% |
| implementing | opencode | 1 | 1 | 1 | 0 | 0 | 0 | 0 | 100% / 0% |

## Per-role recommendation

- **Reviewer: insufficient evidence.** Claude produced two conclusive verdicts in five dispatches; Codex produced one structured `changes_requested`. Junie had no observed terminal marker before the run budget expired, and OpenClaw returned an unknown `error` marker. No harness reached three conclusive reviewer observations.
- **Security: insufficient evidence.** OpenClaw was the only dispatched security candidate and returned `error`. Claude and Codex security were not observed; earlier stages exhausted their cells. There is no security winner.
- **Implementer: insufficient evidence.** Claude opened six PRs across six cells; Codex and OpenCode each opened one. These prove the PR-producing path worked in those samples, not final acceptance or comparative reliability. Keep the current assignments provisional.

## Generated ranking

```text
architecting: INSUFFICIENT_EVIDENCE
  1. claude_code    scalar=0.8319  credit=8/8 conclusive  defects=0  runs=8

assessing: INSUFFICIENT_EVIDENCE
  1. claude_code    scalar=0.9476  credit=8/8 conclusive  defects=0  runs=8

implementing: INSUFFICIENT_EVIDENCE
  1. claude_code    scalar=0.7994  credit=6/6 conclusive  defects=0  runs=6
  insufficient evidence (<3 conclusive): codex, opencode

reviewing: INSUFFICIENT_EVIDENCE
  insufficient evidence (<3 conclusive): claude_code, codex, junie, openclaw

security: INSUFFICIENT_EVIDENCE
  insufficient evidence (<3 conclusive): openclaw

advisory only: no live configuration was modified
```

## Individual cells

Wall-clock totals include teardown beyond the 600-second execution budget. Dispatches below follow artifact registration order, which need not be chronological; use each dispatch timestamp for chronology. No cell completed the fixture end to end.

| Point | Seconds | Observed stages and terminal markers |
| --- | ---: | --- |
| baseline | 611.0 | assessing/claude_code: assessment_complete; architecting/claude_code: plan_committed; implementing/claude_code: pr_opened; reviewing/claude_code: no terminal marker |
| implementer-harness=codex | 610.8 | assessing/claude_code: assessment_complete; architecting/claude_code: plan_committed; reviewing/claude_code: no terminal marker; implementing/codex: pr_opened |
| implementer-harness=opencode | 610.8 | assessing/claude_code: assessment_complete; architecting/claude_code: plan_committed; reviewing/claude_code: changes_requested; assessing/claude_code: no terminal marker; implementing/opencode: pr_opened |
| reviewer-harness=codex | 610.8 | assessing/claude_code: assessment_complete; architecting/claude_code: plan_committed; implementing/claude_code: pr_opened; assessing/claude_code: no terminal marker; reviewing/codex: changes_requested |
| reviewer-harness=junie | 601.6 | assessing/claude_code: assessment_complete; architecting/claude_code: plan_committed; implementing/claude_code: pr_opened; reviewing/junie: no terminal marker |
| reviewer-harness=openclaw | 601.3 | assessing/claude_code: assessment_complete; architecting/claude_code: plan_committed; implementing/claude_code: pr_opened; reviewing/openclaw: error |
| security-harness=codex | 610.8 | assessing/claude_code: assessment_complete; architecting/claude_code: plan_committed; implementing/claude_code: pr_opened; reviewing/claude_code: no terminal marker |
| security-harness=openclaw | 600.5 | assessing/claude_code: assessment_complete; architecting/claude_code: plan_committed; implementing/claude_code: pr_opened; reviewing/claude_code: approved; security/openclaw: error |

The security/OpenClaw cell also logged `security_floor.scan_failed` with `ScannerError` before security dispatch. That environment diagnostic is an additional limitation; the raw dispatch marker remains `error`, and the classifier is not changed to infer a cause.

## Interpretation and evidence floor

The unchanged ranker uses `min_conclusive=3`. A single bounded board traversal gives
a substituted role at most a small number of opportunities, and earlier stages can
consume its entire budget before that role is dispatched. Three was not reachable
for the new candidates in these runs. Repeated Claude observations pooled from the
other cells do not supply missing evidence for the alternatives.

Retain the floor. Future confirmation should obtain at least three completed
exposures per `(role, backend)` and multiple contributing runs, with enough budget
to reach the persona under test or a separately labelled isolated-stage fixture.
Lowering the threshold would turn one observation into a recommendation without
measuring repeatability. The observed rates describe only this fixture and model;
they do not justify a general ranking of the harnesses.

The raw marker `error` is intentionally unknown/inconclusive under the existing
classification contract. OpenClaw's observed errors therefore remain visible without
being relabelled as model failures or parser defects. A timeout or absent marker also
cannot distinguish slow model work, a stuck harness, or a lost terminal observation.
The retained counts are not an estimate of the true underlying defect rate.

Two observation defects are filed separately as explicitly required by #251:
[#305](https://github.com/ViviDynamics/coordinare/issues/305), successful model calls
reported as zero benchmark tokens, and
[#306](https://github.com/ViviDynamics/coordinare/issues/306), a fatal executor error
missing from a real benchmark terminal observation in the companion Switchyard
experiment. This PR measures the harnesses; those issues own repairs. The raw zeros
remain unmodified for reproducibility but cannot support cost or efficiency claims.
Likewise, generated scalar values retain the existing default cost assumptions;
they are not measured economic savings or a recommendation to deploy a winner.

## Reproduction

From the repository root, export the gateway credential required by the launcher
and ensure Docker can run the full performer image. Use a fresh output directory:

```sh
PYTHONPATH=src:agent/performer/src .venv/bin/python specs/161-board-bench-harness/live/bench_harness_live.py --output /tmp/harness-new-run --seconds 600 --repeats 1
PYTHONPATH=src:agent/performer/src .venv/bin/python -m coordinare.bench.harness_rank /tmp/harness-new-run/*/1/run.json
```

The launcher writes no resolved credentials. The evidence directory retains all
eight final `run.json`/`score.json` pairs, provenance, pooled rollup and ranking.
Full container logs are not published because they can contain sensitive runtime
locals. The JSON artifacts are unchanged, including their original timestamps,
fingerprints and incomplete observations. No deployment configuration was modified.
