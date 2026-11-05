# Spike 376: environment discovery authoring on nare

Issue: [#376](https://github.com/ViviDynamics/coordinare/issues/376). Date: 2026-11-03.

Question: can the manifest authoring step of environment discovery run on the nare agent
harness instead of coordinare's own chat loop, at a materially lower token cost and with
no loss in manifest quality, while coordinare keeps the trust layer (pinned source,
evidence provenance, validation, independent review)?

Short answer: it can run, but it was not cheaper. One of the nare runs produced a manifest that coordinare admitted (after 4 of its own correction rounds) and reviewed; the review result was in line with the baseline (`revise`, one finding, against a baseline median of three). Its authoring cost 919,854 input tokens to an admitted manifest, against a baseline median of 553,579 for the first authoring round, and 1,146,530 including the review against a baseline median of 1,106,948. No call on either harness reported a cached token, with or without `--cache-breakpoints`, so the saving the issue hoped for (prefix caching) is not available on this backend. Along the way nare showed five gaps that block or degrade this use; four are filed and one is evidence on an open issue. Recommendation: do not adopt for discovery authoring now (details at the end).

The re-run on nare `ba35b2a` (2026-11-04), at the end of this file, revisits this recommendation.

## Method

Everything ran against the pinned baseline: repository `dettmore-platform` at
`bf498e843ba58362dc6abe122b91129f7c66351f`, the baseline profile's model through the
LiteLLM gateway (both named only in the private profile), reasoning effort low, 32,768 max output tokens, 262,144 context tokens.

**Code under test.** The discovery loop, validators and reviewer exist only on the 353
branch, not on `main`. The spike imports them from a clean detached checkout of
`e15eace0` (the coordinare commit that produced baseline run e2e-50) through `PYTHONPATH`.
nare is `origin/main` at `0501d80` (release `2026.11.3`), run from a detached worktree.

**Spike script.** [`scripts/spikes/nare_discovery.py`](../../scripts/spikes/nare_discovery.py),
not wired into any production path. For one run it:

1. Exports the pinned commit with `git archive` into a read-only directory, used as nare's
   `--root`. The clone's checkout is not touched.
2. Builds the system text from coordinare's own `SOURCE_NAVIGATION`, the capability
   sentences of `SOURCE_TOOLS`, the source-inspection guidance of
   `DiscoveryPrompts.update`, and `_MANIFEST_INSTRUCTIONS`. The JSON tool protocol
   (`read_file`, `read_files`, `list_files`) is replaced by nare's `read`, `list` and
   `search`. Probes are unavailable this round, and the text says so: do not assume an
   uncertain prerequisite, install it or make a required check exercise it. The probe
   sentence of `_MANIFEST_INSTRUCTIONS` is removed. `finding_responses` must be `[]`.
3. Builds the task (`--prompt-file`) keyed like coordinare's discovery message: the
   reviewed scope's 56 criteria (43 required), the scope digest and mapping instruction,
   execution capabilities, platform roles and carrier process policy derived from the
   baseline's capabilities, and the execution rules copied verbatim from the baseline's
   first discovery call. The scope is the accepted scope of e2e-50 attempt 0, so no scope
   phase runs here.
4. Writes coordinare's `_MANIFEST_ENVELOPE.model_json_schema()` for `--schema`. nare refused
   it (see gaps), so the three schema-valued `additionalProperties` (both `env` maps and
   `criterion_checks`) are opened to `true`. nare then checks those maps are objects;
   coordinare still checks their values.
5. Runs `nare run --yes --jsonl --contract 2 --provider openai --tools read,list,search`
   with `--budget-tokens 4000000 --max-turns 80`, capturing the JSONL stream and session.
   A provider failure (`stop_reason` null) is followed by a resume with no new prompt, up to
   40 times per run.
6. Re-derives every file nare read from the pinned Git objects (`git show <commit>:<path>`)
   through coordinare's `SourceInspection`, and compares coordinare's SHA-256 with each
   nare evidence record. It also binds every reviewed-scope input, as `_discover_attempt`
   does before authoring.
7. Admits the answer with coordinare's own `_CandidateValidator` (proposal model,
   `ReviewedConstruction.admit`, static rules, finding responses, project file bounds).
   A refusal is sent back by resuming the nare session with coordinare's own
   `_validation_feedback` and `correction_instruction('manifest')`: up to 3 rounds by
   default, 8 for run 10 (the baseline profile's `max_schema_repairs`). Run 10's correction
   rounds also carried the exact refused envelope (see gap 4).
8. Runs coordinare's independent executable review once on an admitted manifest, through
   `_review` with the divided build from `_divided_construction` (one reviewer per check
   group plus the global part, the production path for a scope of this size), with the same
   model through coordinare's own `OpenAiApiBackend` (`json_object` transport), and records
   the verdict and findings.

Recorded per run: nare usage (input, output, cache_read), turns, wall time, provider
failures and resumes, admission result and feedback, review usage, verdict and findings,
and evidence completeness. Private raw artifacts are under the worktree's gitignored
`.agents/state/376-nare-spike/` (run directories `run-1` to `run-13` with their JSONL streams,
sessions, answers and review transcripts, `run-10/rejudge-4/` for the review, the baseline
summary `baseline-summary.json`, and the launchers `launch.sh` and `rejudge.sh`). Only sanitized numbers are here.

### Commands

The key is passed only through the environment, never in argv or a file:

```sh
# Baseline summary (no model calls)
python scripts/spikes/nare_discovery.py baseline \
  <353 worktree>/.agents/state/353-org-validation/dettmore-e2e-{02..50} \
  --out .agents/state/376-nare-spike/baseline-summary.json

# One spike run (runs 8 to 13 added --stream; 3, 7, 9, 11 and 12 --cache-breakpoints;
# 13 --structured-output)
OPENAI_API_KEY="$(cat <key file>)" \
PYTHONPATH=<e15eace0 checkout>/src:<e15eace0 checkout>/packages/service_inference/src \
python scripts/spikes/nare_discovery.py run \
  --clone <dettmore-platform clone> --commit bf498e843ba58362dc6abe122b91129f7c66351f \
  --reference <353 state>/dettmore-e2e-50/dettmore-platform/attempt-000 \
  --baseline-first-discovery <353 state>/dettmore-e2e-50/dettmore-platform/chat-043.json \
  --nare <nare 0501d80 worktree> --base-url <gateway>/v1 \
  --model <model> --max-transport-resumes 40 --stream \
  --out .agents/state/376-nare-spike/run-N

# Continue a run from its last answer through the repair loop (run 10, twice)
python scripts/spikes/nare_discovery.py run <same flags> --stream --continue --max-repairs 8 \
  --out .agents/state/376-nare-spike/run-10

# Judge a saved answer again: validation only (run 6), or with the divided review (run 10)
python scripts/spikes/nare_discovery.py rejudge [--skip-review] <shared flags> \
  --run .agents/state/376-nare-spike/run-10 --round 4
```

The nare command the script builds (paths shortened):

```sh
uv run --project <nare> nare run --yes --jsonl --contract 2 --provider openai \
  --base-url <gateway>/v1 --model <model> \
  --max-tokens 32768 --effort low --context-window 262144 --budget-tokens 4000000 \
  --max-turns 80 --tools read,list,search --root <export> --schema <schema> \
  --prompt-file task.json --session session.json --system "<system text>" \
  [--stream] [--cache-breakpoints]
```

## Baseline

47 runs, dettmore-e2e-02 to e2e-50 (e2e-30 and e2e-31 recorded no usage), computed from
the saved `chat-*.json` files by the script's `baseline` command. A baseline run is a
whole attempt chain: sharded scope review, authoring, review, probes, installer runs and
execution repairs. 44 held, 3 reached `accepted_not_executed`. Cached tokens were 0 on
every call of every run. Median input per call was 78k tokens (range 67k to 91k).

| Baseline, per run | Median | Range |
| --- | ---: | ---: |
| Model calls | 108 | 23 to 180 |
| Input tokens | 8,328,631 | 1,845,981 to 16,240,820 |
| Output tokens | 141,696 | 7,693 to 374,422 |
| Cached tokens | 0 | 0 to 0 |
| Wall time (s) | 6,191 | 939 to 15,359 |

| Baseline, per phase per run | Runs | Calls (median) | Input (median, range) | Output (median) |
| --- | ---: | ---: | ---: | ---: |
| scope_shard_review | 47 | 41 | 3,539,590 (1,845,981 to 6,877,131) | 23,439 |
| scope_merge_review | 46 | 5.5 | 315,902 (53,500 to 2,287,397) | 2,368 |
| scope_revision | 24 | 3 | 167,839 (52,481 to 1,522,118) | 2,072 |
| discovery (global author, all rounds) | 44 | 10.5 | 1,110,982 (100,338 to 3,993,917) | 36,403 |
| executable_checks (check authors, all rounds) | 37 | 18 | 840,369 (119,185 to 1,749,201) | 50,763 |
| review (all rounds) | 33 | 29 | 2,203,045 (635,841 to 6,981,511) | 42,261 |

The comparable slice is the **first authoring round** (from the first `discovery` call up
to the first `review` call, `discovery` plus `executable_checks`) and the **first review**
(the first contiguous block of `review` calls):

| Baseline slice | Runs | Calls | Input (median, range) | Output (median, range) |
| --- | ---: | ---: | ---: | ---: |
| First authoring round | 44 | 9 (2 to 17) | 553,579 (100,338 to 1,079,580) | 30,264 (875 to 54,083) |
| First review | 33 | 7 (4 to 10) | 495,465 (307,043 to 861,384) | 12,145 (5,773 to 29,969) |
| First round plus first review | 33 | | 1,106,948 (799,949 to 1,578,428) | 45,133 (31,100 to 69,796) |

The baseline's first authoring round includes a probe preflight (the global author must
submit a probe batch before a manifest), which the nare runs do not have.

Baseline first-review quality: in 0 of 33 runs did every review part accept. Each first
review raised a median of 3 new blocking findings (range 1 to 12), mostly
`execution_definition`, `coverage` and `activation`. So a first-round `revise` is the
baseline norm, not a regression.

## nare runs

Every run started is listed. "Provider failures" are requests that ended a nare run with
`stop_reason` null (`ReadTimeout` at nare's fixed 600 s, or `ReadError`); the script
resumed the session after each.

Columns: invocations of nare (a resume is an invocation), completed turns, nare input and
output tokens, `cache_read`, provider failures, distinct files the model read, and how the
run ended. No run used more than 25% of its 4,000,000 token budget or hit `--max-turns`.

| Run | Flags | Inv. | Turns | Input | Output | Cache read | Provider failures | Files read | End |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | | 1 | 6 | 173,436 | 248 | 0 | 1 | 9 | `ReadTimeout`; no resume in the script yet |
| 2 | | 2 | 5 | 150,409 | 6,754 | 0 | 1 | 17 | stopped (stalling, too few resumes allowed) |
| 3 | breakpoints | 2 | 4 | 116,260 | 8,310 | 0 | 1 | 16 | stopped (same) |
| 6 | | 5 | 8 | 324,795 | 38,009 | 0 | 2 | 2 | answered; refused by a spike bug, then `schema_violation` |
| 7 | breakpoints | 7 | 17 | 617,487 | 27,753 | 0 | 6 | 34 | stopped (switched to `--stream`) |
| 8 | stream | 1 | 2 | 52,310 | 90 | 0 | 0 | 3 | stopped (scope binding bug found) |
| 9 | stream, breakpoints | 1 | 1 | 25,835 | 3,186 | 0 | 0 | 5 | stopped (same) |
| **10** | **stream** | **5** | **15** | **919,854** | **128,168** | **0** | **0** | **44** | **admitted on answer 5; reviewed** |
| 11 | stream, breakpoints | 2 | 5 | 169,770 | 29,043 | 0 | 0 | 0 | refused (`runtime_credential_reference`), then `schema_violation` |
| 12 | stream, breakpoints | 1 | 14 | 483,430 | 53,498 | 0 | 0 | 26 | `schema_violation` (prose, then a document with prose after it) |
| 13 | stream, structured output | 6 | 11 | 458,019 | 80,406 | 0 | 5 | 37 | first answer refused by nare's schema check; constrained correction turn and 5 resumes lost to `ReadError`; stopped |

Runs 4 and 5 do not exist as results: a run-4 start was stopped within a minute (see
caveats) and the label run-5 was never used. Total nare spend across runs 1 to 13:
3,491,605 input and 375,465 output tokens, plus 226,676 input and 11,533 output for run 10's review.

Run 10 in detail:

| Run 10 | Turns or calls | Input | Output | Cache read | Wall time |
| --- | ---: | ---: | ---: | ---: | ---: |
| nare, to the first answer | 11 turns | 483,710 | 86,029 | 0 | 6,437 s |
| nare, 4 correction rounds (resumes) | 4 turns | 436,144 | 42,139 | 0 | 3,218 s |
| nare, to the admitted manifest | 15 turns | 919,854 | 128,168 | 0 | 9,655 s |
| coordinare review (divided, 3 parts) | 6 calls | 226,676 | 11,533 | 0 | 536 s |
| Total | | 1,146,530 | 139,701 | 0 | |

The four refusals, in order, all from coordinare's own validator: `scope_id_collision`
twice (requirement ids reused scope criterion ids), then `clean_dependency_install_only`
twice (a check or service depended on an install step). The first refusal is the one
coordinare's global author instructions pre-empt (`GLOBAL_AUTHOR_INSTRUCTION` forbids
criterion ids as record ids); the spike's single-author prompt did not carry that
sentence. Every run that answered (6, 10, 11, 12, 13) reused criterion ids as requirement ids.

## Comparison

The comparable slices, run 10 against the baseline (47 runs; medians, ranges in brackets):

| Slice | nare (run 10) | Baseline | nare / baseline median |
| --- | ---: | ---: | ---: |
| Authoring, input tokens, to the first answer | 483,710 | 553,579 [100,338 to 1,079,580] (first authoring round) | 0.87 |
| Authoring, input tokens, to an admitted manifest | 919,854 | 553,579 (the baseline round ends admitted) | 1.66 |
| Authoring, output tokens, to an admitted manifest | 128,168 | 30,264 [875 to 54,083] | 4.2 |
| Authoring, model calls | 15 turns | 9 [2 to 17] | 1.7 |
| Input per call | 61,324 (mean) | 78,073 [67,293 to 91,091] (per-run mean) | 0.79 |
| First review, input tokens | 226,676 | 495,465 [307,043 to 861,384] | 0.46 |
| First review, calls | 6 | 7 [4 to 10] | |
| Authoring plus first review, input | 1,146,530 | 1,106,948 [799,949 to 1,578,428] | 1.04 |
| Authoring plus first review, output | 139,701 | 45,133 [31,100 to 69,796] | 3.1 |
| Cached tokens | 0 | 0 | |

What these say:

- **No saving on authoring.** nare's turns carry less per call (no 100 KB scope-input index and
  no probe evidence in the prompt), so its first answer cost a little less than a baseline first
  round. But every correction is a resume that resends the whole transcript and makes the model
  write the whole envelope again (each of run 10's five answers is about 38 KB as saved);
  coordinare's own loop can ask for a correction as a patch (`PATCH_INSTRUCTION`) instead. To an
  admitted manifest, nare cost 1.66 times the input and 4.2 times the output.
- **The review difference cannot be credited to nare.** Both are coordinare's divided reviewer on
  the same model, and one run is inside the noise of 33 baseline first reviews; the manifest's
  shape may influence how much a reviewer reads, but n = 1 cannot show it.
- **Caching does not change the picture on this backend.** With and without `--cache-breakpoints`,
  `cache_read` was 0 on all 88 nare turns, as on every baseline call. nare keeps a stable prefix,
  so on a caching backend its resent history would be cheaper; this gateway and model did not
  report a single cached token for either harness.
- **The large costs are elsewhere.** Scope review is a median 4.0M input tokens per baseline run,
  and later review and repair rounds most of the rest. The authoring step this spike moved is a
  median 0.55M of a median 8.3M per run.
- **Per-run totals are not comparable** and are not compared: the nare runs stop at one review.

## Quality outcome

Run 10, the one complete run:

- **Admission.** Admitted by coordinare's `_CandidateValidator` on the fifth answer (four repairs
  through resume, within the baseline's 8-repair allowance). The admitted manifest has 42
  requirements, 8 install commands, 30 checks, 1 service, and maps all 43 required criteria.
- **Independent review.** coordinare's divided review (2 check groups plus the global part, the
  production path for this scope) returned `revise` with one blocking finding: activation does not
  export `BUNDLE_PATH`, so plain project commands in 16 checks would not resolve the installed
  bundle. Two parts accepted, one revised. The baseline's first review never accepted outright
  (0 of 33) and raised a median of 3 findings (1 to 12), with `activation` among the most common
  categories. On this single run, quality is in the baseline's range, not below it.
- **Evidence.** The model read 44 distinct files; coordinare re-derived all 44 from the pinned Git
  objects and every digest matched nare's evidence record (0 mismatches). Of the 34 paths the
  manifest cites, 24 were read whole, 1 partly (`Gemfile.lock`, read in pages that did not cover
  the whole file), and 9 were never read by the model; they passed admission because the daemon
  binds every reviewed-scope input before authoring. No read fell outside the reviewed scope's inputs.
- **An undivided review does not fit.** The spike first ran coordinare's single reviewer, which
  failed with HTTP 400: its opening message was 924 KB, because it carries the 370 KB scope
  contract twice (identity and manifest). That is a property of coordinare's single-reviewer path
  for a sharded scope, not of nare.

The other answering runs did not reach review: run 6 answered from 2 files and run 11 from none
(both copied the scope criteria into requirements), run 12's two final replies were not
parseable (see gaps 4 and 5), and run 13's first answer was refused by nare's schema check (its
saved copy ends before the manifest closes, so it appears incomplete; redaction prevents checking
the raw text) and its correction turn never completed. Quality is therefore measured on n = 1.

## nare gaps found

1. **Generated schemas with map fields are refused.** coordinare's envelope schema, generated
   by pydantic, was refused before any model call: `schema keyword 'additionalProperties' at
   #/$defs/EnvironmentManifestProposal.criterion_checks must be true or false, got object`.
   Three fields use a schema-valued `additionalProperties` (`Activation.env`, `Command.env`,
   `criterion_checks`). The contract says generated pydantic schemas validate without edits.
   The spike opened the three maps to `true`. Filed as
   [nare#131](https://github.com/ViviDynamics/nare/issues/131).
2. **One slow or dropped request ends the run.** nare's OpenAI transport sends each turn as
   one request with a fixed 600 s timeout and no retry. On the shared gateway (where the
   concurrent e2e-51 validation's calls took a median 294 s and up to 1,668 s), the five
   unstreamed runs had 11 provider failures (9 `ReadTimeout`, 2 `ReadError`) against 40
   completed turns; run 7 alone needed 6 resumes for 17 turns. coordinare's own client
   times out at 180 s, streams, and retries transient failures for up to 10 minutes.
   `--stream` removed the timeouts in runs 8 to 12 (0 provider failures in 37 streamed turns).
   It does not help a dropped connection: in run 13 the schema-constrained correction turn and
   then five resumes in a row ended in `ReadError` while the gateway's streaming path returned no
   bytes at all (a direct streamed one-line request also timed out at 300 s, while the same
   request unstreamed answered in 62 s). A resume also loses the constrained turn, since nare
   keeps the correction round per run ([comment](https://github.com/ViviDynamics/nare/issues/134#issuecomment-6622894914)). Filed as
   [nare#134](https://github.com/ViviDynamics/nare/issues/134).
3. **The system prompt is argv only and is not kept in the session.** A `--resume` without
   `--system` silently sends nare's default prompt. The spike passes the multi-kilobyte
   system text on every invocation. Filed as
   [nare#132](https://github.com/ViviDynamics/nare/issues/132).
4. **Redaction corrupts structured answers in the saved transcript.** The
   `credential-assignment` rule matched `${NAME}` references (no secret anywhere) 8 times in
   run 11's answer and left it unparseable. On `--resume` (the only way to send coordinare's
   validation feedback back) the model sees the corrupted copy, and in run 11 it reasoned
   about the `[redacted]` markers as its own error. The parsed `output` field is unaffected.
   This is the open [nare#68](https://github.com/ViviDynamics/nare/issues/68); the evidence
   is added in [a comment](https://github.com/ViviDynamics/nare/issues/68#issuecomment-6620722585).
   For run 10's correction rounds the spike put the exact refused envelope in the feedback
   prompt, so the model had an intact copy.
5. **A JSON answer followed by a sentence is refused as "no JSON found".** `extract_json`
   accepts the whole reply or a fenced block only. In run 11 a repaired answer was an
   18 KB document followed by a line of prose; with one correction turn per run, two such
   replies ended it with `schema_violation`. Filed as
   [nare#133](https://github.com/ViviDynamics/nare/issues/133).

Not nare gaps, but findings:

- `--cache-breakpoints` was accepted by the gateway and changed nothing: `cache_read` was 0
  on every turn of runs 3, 7, 9, 11 and 12, as it is on every baseline call. This backend
  does not report or does not do prefix caching, so caching cannot be the saving here.
- nare has no way to require reading before answering. coordinare enforces a
  `source_inspection` phase before a manifest is accepted; a single nare session can only
  be told to. In runs 6 and 11 the model answered after reading 2 and 0 files.
- coordinare's admission does not require the author to have read what it cites either:
  the daemon binds every reviewed-scope input as inspected before authoring, so citing a
  bound but unread file passes. The spike measures this separately (cited and actually read).

## Caveats

- **No probes, no installer, no clean execution.** The nare runs end at admission and one
  review. The baseline's per-run totals include probe batches, installer runs and execution
  repairs, so per-run totals are not comparable; only the first-round slices are.
- **Different stage boundaries.** The baseline divides authoring into a global author and
  parallel check authors, and divides review into parts; the spike has one nare session and
  coordinare's single (undivided) reviewer, whose opening message carries the whole 370 KB
  scope contract. The baseline's first round includes a probe preflight.
- **Fixed scope.** Every nare run reuses e2e-50's accepted scope. The scope phases, which
  are the largest baseline cost (median 4.0M input tokens per run across the three scope
  phases), are out of this spike's reach.
- **Small n.** One run reached review. The others were spent finding and fixing spike-side problems or ended on the nare gaps above. A single run cannot establish a cost or quality distribution; the baseline has 47.
- **Gateway contention.** Another session's dettmore-e2e-51 validation ran against the same
  gateway and model for the whole spike, with up to 12 concurrent calls. Wall times here
  are dominated by queueing and are not a measure of either harness. Late in the spike the
  gateway's streaming path stopped returning bytes, which ended run 13.
- **Spike-side defects, fixed between runs** (each is why a run was restarted or rejudged):
  no transport resume (run 1); zsh did not split the launcher arguments (two launches that
  never started nare, not counted as runs); too few resumes and no `--stream` (runs 2, 3, 6
  to 9 stopped); the scope inputs were not bound before admission, so every answer failed
  `scope_identity_mismatch` (runs 6, 8, 9; run 6 was rejudged); no source-inspection
  guidance (runs 1 to 9); coordinare's 3,600 s attempt clock started with the run, so it had expired
  when run 10's first answer arrived after 6,437 s (fixed; run 10 was continued from its saved
  session with `--continue`, first with 3 and then with 8 repairs); the undivided reviewer's
  message overran the context (HTTP 400; run 10's review was rerun with `rejudge` on the divided
  path). A run-4 start was stopped within a minute and its directory was
  deleted before its usage was read, so its spend (at most one or two turns) is not recorded.

## Recommendation

**Do not adopt nare for discovery authoring now.**

The spike's question had two parts, and the answer to the first is the decisive one:

1. *Materially lower token cost:* not shown. Authoring to an admitted manifest cost more on nare
   (1.66 times the input, 4.2 times the output of the baseline median first round), and authoring
   plus review was at parity (1.04 times the input). The mechanism the issue relied on, prefix
   caching, gave nothing on this backend for either harness. Fixing the nare gaps would not change
   that: they cost runs, not tokens per run.
2. *No loss in quality:* plausible on n = 1. Run 10's manifest passed coordinare's validator and
   drew one review finding, inside the baseline's range. Evidence provenance held: every read
   digest matched.

What would change the recommendation:

- A serving route that reports prefix cache hits. Then nare's stable prefix (and
  `--cache-breakpoints`) could make resent history cheap, and the comparison is worth rerunning.
  The same change would also benefit coordinare's own loop if it stopped rewriting `messages[0]`
  and `messages[1]`, which is the cheaper experiment.
- nare fixing [#131](https://github.com/ViviDynamics/nare/issues/131) (map schemas),
  [#134](https://github.com/ViviDynamics/nare/issues/134) (transient failures),
  [#132](https://github.com/ViviDynamics/nare/issues/132) (system prompt on resume),
  [#133](https://github.com/ViviDynamics/nare/issues/133) (prose around JSON) and
  [#68](https://github.com/ViviDynamics/nare/issues/68) (redaction of `${NAME}` references).
  Until then a caller has to work around all five, as this script does.
- A correction path cheaper than a full resume and a full re-emitted answer, for example
  coordinare's patch corrections expressed as a follow-up prompt, which is caller-side work.

If the goal becomes harness consolidation rather than tokens, the spike supports a follow-up
that keeps coordinare's divided authoring structure (global author plus group authors, each a
nare session with the corresponding instructions) and adds probes through nare, measured over
several runs. That is a different question from this one.

## Re-run on nare ba35b2a (2026-11-04)

**Status: two of three planned in-loop runs completed. Run 3 did not start.** The gateway key file that the launcher reads (under `~/.t3/userdata/secrets/files/`) is no longer present, so the launcher cannot authenticate. No other credential was substituted. Runs 1 and 2 are below. The recommendation is revisited at the end of this section.

### Short answer

nare's new features close most of the gaps this spike found, and the cost picture moved a long way. Authoring to an admitted manifest now costs 1.08 to 1.24 times the baseline input (was 1.66), and authoring plus the first review costs 0.75 to 0.80 times (was 1.04). Output stays at 1.9 to 3.9 times the baseline (was 3.1 to 4.2). Both admitted manifests drew no blocking finding from coordinare's review, where the baseline never accepted on its first review (0 of 33). One of the two left cited files unread, and no run shows a cache hit. Two runs cannot separate a real saving from run-to-run variation (the two runs differ by 6 model turns and 2.4 times in citations).

### Setup

- **nare.** `origin/main` at `ba35b2a` (release 2026.11.26), in a detached worktree (`nare-spike-2`, removed at the end; the SHA is the record). The old `nare-spike` worktree at `0501d80` is untouched.
- **Pinned baseline and model.** Unchanged from the original runs: `dettmore-platform` at `bf498e8`, the reference scope and capabilities from e2e-50 attempt 000, the first discovery call (chat-043) for the execution rules, `spark/glm-5.3-flash` through the same gateway, reasoning effort low, 32,768 max output tokens, 262,144 context tokens, a 4,000,000 token budget and 80 turns. Streaming is on, as in runs 8 to 12.
- **Flags.** `--max-corrections 8` (the baseline profile's repair allowance, shared by schema, check and read corrections), `--require-reads 20` (about half of run 10's 44 distinct reads, fixed before the runs), nare's default retries (3, 60 s apart) and its 600 s request timeout. The envelope schema is coordinare's, unmodified, because nare now accepts map-valued `additionalProperties` (#131).
- **Mode.** The script's `inloop` subcommand (the `run` mode is unchanged). nare's Python API takes `check=` (the CLI cannot). The check is coordinare's `_CandidateValidator`, one object for the whole run, as production keeps one across repair rounds. Before each validation it binds every file nare has read so far, from a worker thread. A refusal goes back to nare as check errors (code, location, message, and guidance carrying coordinare's correction instruction on the first error). An admitted envelope goes through the same divided review as `run`. Nothing is resumed through the CLI.
- **Workaround for nare#192.** `_check_passes` lets a `ValueError` escape on a tool turn with no JSON when `check` is set. The spike wraps that one function in its own process and records `nare_192_workaround: true` in every record.
- **Environment.** One Python 3.12 venv with coordinare's pinned dependencies and nare installed editable from `nare-spike-2`. nare needed `anthropic` 2.5.0 in place of 0.79.0. The unit tests (`tests/unit/test_376_nare_spike_evidence.py`) pass under the launcher's venv.
- **Command.** `bash .agents/state/376-nare-spike/launch-inloop.sh inloop-a1 --stream --max-corrections 8 --require-reads 20`. The key is read from its file inside the command.
- **Offline checks (no model calls).** A replayed run admitted the run 10 envelope on the first check (42 requirements, 8 install, 30 checks, 43 criteria mapped, the same shape as run 10). A replayed envelope with a wrong citation was refused by coordinare and then admitted after nare's correction. A replayed tool read was digest-matched. `--require-reads` held an answer with 2 of 3 reads. A JSON answer followed by prose finished `done` with a check set (#133).

### Results

Baseline is the median across the 47 saved runs (first authoring round and first review, as in the table above). Run 10 is the original nare run. Runs a1 and a2 are the in-loop runs. "To admitted" is the envelope the run ended on.

| Metric | Baseline (median) | nare run 10 (0501d80, CLI) | in-loop a1 | in-loop a2 |
| --- | ---: | ---: | ---: | ---: |
| nare model calls (turns), to admitted | 9 (first round) | 15 | 16 | 10 |
| Input tokens, authoring to admitted | 553,579 | 919,854 | 687,656 | 600,618 |
| Output tokens, authoring to admitted | 30,264 | 128,168 | 117,284 | 67,481 |
| Input, authoring plus first review | 1,106,948 | 1,146,530 | 831,778 | 888,515 |
| Output, authoring plus first review | 45,133 | 139,701 | 125,052 | 85,615 |
| Cache-read tokens | 0 | 0 | 0 | 0 |
| Corrections: nare (charged, free) | n/a | 4 (all charged) | 4 charged (1 non-JSON, 3 coordinare refusals) | 4 charged, 1 free (5 coordinare refusals), plus 1 charged read hold |
| Coordinare refusals before admission | n/a | 4 | 3 | 5 |
| Distinct files read | n/a | 44 | 35 (25 whole, 10 partial) | 23 (all whole) |
| Cited paths (unread / partial) | n/a | 34 (9 / 1) | 36 (13 / 8) | 15 (0 / 0) |
| Digests matched (of files read) | n/a | 44 of 44 | 35 of 35 | 23 of 23 |
| Admitted by coordinare | n/a | yes | yes | yes (the 6th check was admitted and then held for reads; the 7th was final) |
| Review verdict (divided, first pass) | 0 of 33 accept; median 3 findings | revise, 1 finding | accept, 0 findings | accept, 0 findings |
| Review input, output | 495,465, 12,145 | 226,676, 11,533 | 144,122, 7,768 | 287,897, 18,134 |
| Wall time to admitted (s) | n/a (whole baseline run: median 6,191) | 9,655 | 5,210 | 1,614 |
| Provider failures or retries | n/a | 0 | 0 | 0 |

Ratios to the baseline first round plus first review: authoring input 1.66 (run 10), 1.24 (a1), 1.08 (a2). Authoring output 4.23, 3.88, 2.23. Authoring plus review input 1.04, 0.75, 0.80. Authoring plus review output 3.10, 2.77, 1.90.

The baseline's first round includes a probe preflight that the nare runs omit, so the comparison favours nare slightly.

### Gap status

| Gap (spike) | Status on ba35b2a | Evidence |
| --- | --- | --- |
| Map-valued schemas refused ([#131](https://github.com/ViviDynamics/nare/issues/131)) | Closed | `check_supported` accepts the unmodified envelope schema. Both in-loop runs used it unchanged. |
| One slow or dropped request ends the run ([#134](https://github.com/ViviDynamics/nare/issues/134)) | Closed in code, not exercised | Retries (3, 60 s) and a configurable timeout exist. Neither run had a provider failure, so the retry path was not tested under load. |
| System prompt not kept for a resume ([#132](https://github.com/ViviDynamics/nare/issues/132)) | Closed | The session records the system prompt. The in-loop path does not resume at all. |
| `${NAME}` redaction corrupts the saved answer ([#68](https://github.com/ViviDynamics/nare/issues/68)) | Open upstream, bypassed | The saved transcript is still redacted. In-process, the model sees its own answers unredacted, so corrections do not depend on the copy. |
| Prose around a JSON answer refused ([#133](https://github.com/ViviDynamics/nare/issues/133)) | Closed | Offline: JSON followed by prose with a check set finished `done`. Neither live run hit this case. |
| Corrections cost a resume each | Closed as a mechanism | Corrections run in one session with no resume and no spike-side repair loop. See the cost note below. |
| Answer after few or no reads (runs 6 and 11) | Partly closed | `--require-reads` holds a valid answer (a2: held at 4 reads, then 19 more reads before the final answer). It counts partial reads as reads, and it only applies once an answer passes validation. In a2 the first five answers came after 4 reads and were refused by coordinare, not held. |
| Citations of files never read (runs 10 and 11) | Open | Run 10: 9 of 34 unread. a1: 13 of 36 unread, despite 35 reads. `--require-reads` counts files, not the files a manifest cites. a2 had 0 of 15 unread. |
| Caching (`--cache-breakpoints`, prefix cache) | Still open | `cache_read` was 0 on every turn of both runs. |
| `check=` crashes on a tool turn with no JSON | New, [#192](https://github.com/ViviDynamics/nare/issues/192) | Found while building the in-loop mode. Workaround in the spike (see Setup). |

### Cost note

The original section blamed the cost of correction on the resume resending the whole transcript. That is only part of the story. Every request in either harness carries the whole transcript, so moving corrections in-process does not remove that cost. What changed is the number of turns and what the model wrote. The in-loop runs still rewrote whole envelopes (output 67,481 to 117,284 tokens), so most of the output gap is the model's writing, not the harness. The two in-loop runs differ from each other by 87,038 input tokens to admission (600,618 and 687,656), more than a2's whole gap to the baseline (47,000). The difference from run 10 is therefore not attributable to the correction mechanism on this evidence.

### Quality

- Both in-loop manifests were admitted by coordinare's validator and drew no finding on the divided review. The baseline's first review never accepted outright. This is the strongest quality signal so far, but it is n = 2, the reviewer had no probe evidence, and it is not evidence that the manifests are better.
- Evidence provenance held for every file read: 35 of 35 and 23 of 23 digests matched coordinare's re-derivation from the pinned Git objects.
- a1 cited 13 files it never read (among them `Gemfile.lock`, `package-lock.json`, `spec/rails_helper.rb`). Coordinare admitted them because the daemon binds every reviewed-scope input before authoring, as in the original runs. This is coordinare's admission rule working as designed, and it means the citation gap is a property of the policy, not only of nare.
- The check callable saw an answer that nare then held for reads (a2, check 6). A caller using `check` for its own state (here, the validator's obligation ledger) therefore sees "admitted" before the run's final answer. The final answer was the 7th check.

### Issues filed

- [ViviDynamics/nare#192](https://github.com/ViviDynamics/nare/issues/192): `check=` ends the run when a tool turn's text has no JSON. The defect is in `_check_passes`, which lets `extract_json`'s `ValueError` escape. The repro runs as written on `ba35b2a`.

Checked before filing: no open or closed issue covered `_check_passes` or the `check` tool-turn path.

### Recommendation

**The recommendation changes in substance, but not in action: still do not adopt discovery authoring on nare now.** The old reason was that nare was not cheaper. On two in-loop runs it is close to baseline on input (1.08 to 1.24 times for authoring) and below baseline on authoring plus review (0.75 to 0.80 times), and the two admitted manifests passed review on the first pass. Output is still 2 to 4 times the baseline, and two runs cannot show a saving that is larger than the spread between them. The reason moves from "not cheaper" to "not yet shown", and the next step moves from "fix nare" to "finish the measurement".

To finish it:

1. Run the third in-loop run (blocked on the gateway key file), then two more if the spread stays this wide. Stop at five.
2. Decide whether to enforce that cited files were read. That is a coordinare policy question, and the `check` callable could enforce it in the spike. Keep it out of the baseline comparison until the policy is settled.
3. Fix nare#192 upstream. The spike workaround is only for measurement.

### Caveats

- **n = 2.** One run is missing. a1 and a2 differ by 6 model turns, 2.4 times in citations, and 3,596 s of wall time (5,210 s and 1,614 s).
- **Same fixed scope and no probes.** As in the original runs. The baseline's per-run totals include probes, installs and execution repairs, so only the first-round slice is comparable.
- **Prompt gap kept on purpose.** The spike prompt still omits `GLOBAL_AUTHOR_INSTRUCTION`, so every run is refused for reusing criterion ids (`scope_id_collision`). That is the spike's prompt, not nare.
- **Read threshold chosen before the runs.** 20 was about half of run 10's reads. a2 shows the threshold holding a valid answer after 4 reads, and a1 read 35 without a hold.
- **Nothing is cached.** `cache_read` was 0 on every turn.
- **Crashed first attempt.** A first launch stopped on a missing argument (`--request-timeout`) before any model call. Its directory was deleted. It is not counted.
