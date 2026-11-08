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

## Re-run on nare ba35b2a (2026-11-04 and 2026-11-05)

**Status: five in-loop runs. Three were admitted and reviewed (a1, a2, a3). One was aborted by a harness bug (a4, my cut-off default). One failed on nare after eight corrections (a4r). The run cap of five is reached.** The gateway key moved to a new file mid-round, and the launcher now reads that file. The recommendation holds, for reasons that changed (see the end of this section).

nare main has since moved to contract 3 and closed #192. Re-pinning `inloop` needs `--contract 3` on the CLI path. Check the Python API for the same field before re-pinning, and drop the #192 workaround then. Nothing was re-pinned in this round.

### Short answer

Authoring to an admitted manifest now costs 0.89 to 1.24 times the baseline input (median 1.08; was 1.66 on run 10). Authoring plus the first review costs 0.67 to 0.80 times (median 0.75; was 1.04). Output stays at 1.96 to 3.88 times the baseline (was 3.1 to 4.2). Two of three admitted manifests drew no finding on the divided review, and one drew five findings, mostly coverage. The baseline's first review never accepted (0 of 33). Reliability is the weak point: two of five runs did not admit, and both failures came from nare or the harness, not from the model's manifest quality. The recommendation holds: do not adopt discovery authoring on nare yet.

### Setup

- **nare.** `origin/main` at `ba35b2a` (release 2026.11.26), in a detached worktree (`nare-spike-2`, removed at the end). Recreated at the same commit the a1 and a2 runs used, so the runs are comparable.
- **Pinned baseline and model.** Unchanged: `dettmore-platform` at `bf498e8`, the e2e-50 reference scope and capabilities, the chat-043 execution rules, `spark/glm-5.3-flash` through the same gateway, reasoning effort low, 32,768 max output, 262,144 context, a 4,000,000 token budget, 80 turns, streaming on.
- **Flags.** `--max-corrections 8` (shared by schema, check and read corrections), `--require-reads 20`, nare's default retries (3, 60 s) and 600 s request timeout, and `--max-cutoffs 2` (see a4 below). The envelope schema is coordinare's, unmodified.
- **Mode.** The script's `inloop` subcommand. nare's Python API takes `check=`, which the CLI does not. The check is coordinare's `_CandidateValidator`, one object for the whole run. Before each validation it binds every file nare has read so far. A refusal goes back to nare as check errors. An admitted envelope goes through the same divided review as the `run` mode.
- **Workaround for nare#192 (closed upstream).** `_check_passes` lets a `ValueError` escape on a tool turn with no JSON when `check` is set. The spike wraps that function in its own process, and each record says so.
- **Environment.** One Python 3.12 venv with coordinare's pinned dependencies and nare installed editable from `nare-spike-2`. nare needed `anthropic` 2.5.0 in place of 0.79.0.
- **Command.** `bash .agents/state/376-nare-spike/launch-inloop.sh <label> --stream --max-corrections 8 --require-reads 20`. The key is read from its file inside the command, never printed.

### Runs

| Run | Outcome | nare turns | Input to end | Output to end | Review input, output | Refusals | Corrections (charged, free, read hold) | Files read | Cited (unread, partial) |
|---|---|---:|---:|---:|---:|---:|---|---:|---|
| a1 | admitted, accept | 16 | 687,656 | 117,284 | 144,122, 7,768 | 3 | 4, 0, 0 | 35 | 36 (13, 8) |
| a2 | admitted, accept | 10 | 600,618 | 67,481 | 287,897, 18,134 | 5 | 4, 1, 1 | 23 | 15 (0, 0) |
| a3 | admitted, revise (5 findings) | 10 | 491,997 | 79,695 | 247,417, 8,847 | 2 | 3, 0, 0 | 34 | 22 (3, 0) |
| a4 | aborted (harness: cut-off off) | 5 | 112,328 | 3,655 | not run | 0 | none | 14 | n/a |
| a4r | failed: schema_violation | 14 | 817,031 | 75,424 | not run | 0 | 8, 0, 0 (budget spent) | 11 | n/a |

**a2 detail.** The first valid-looking answer (check 6, turn 8) was held for reads: four distinct files had been read. The model then read 19 more files and answered again (check 7, turn 10). The five earlier answers were refused by coordinare, not held, and each came after the same four reads. So `--require-reads` acted only once an answer passed validation, as the flag is documented to.

**a3 findings.** Five findings: four `coverage` and one `execution_definition`. The manifest had 24 requirements and 24 checks, against 42 to 43 and 29 in a1 and a2. The smaller manifest is a plausible source of the coverage findings, but n = 3 cannot show it.

**a4 (aborted).** Stopped at turn 5 by nare's repetition cut-off, with no coordinare check and no correction. The cause was mine: in the Python API, `max_cutoffs=None` creates no cut-off limit, and the CLI default is 2. The spike now defaults `--max-cutoffs` to 2 and says so in the help text. Its 116k tokens are counted in the spend below and not in the comparison.

**a4r (failed).** Admission failed after 8 charged corrections, all saying the answer "ends before it closes" or had no JSON. The saved transcript shows the answer (16 to 52 KB) broken at the `local_credentials` key, after `runtime_endpoints`. The rest of the answer is replaced by `[redacted]`. That key name matches nare's `credential-assignment` rule. This is the open #68 (redaction), not a model failure, and it is not bypassed in-process as I wrote earlier. A small offline replay with the same key parsed correctly, streamed or not. I have not reproduced the failure offline. A comment with the evidence is on #68.

### Comparison

Baseline is the median across the 47 saved runs (first authoring round and first review). Run 10 is the original CLI run on `0501d80`. "a1 to a3" are the admitted in-loop runs.

| Metric | Baseline (median) | nare run 10 (CLI) | a1 | a2 | a3 | in-loop median (a1 to a3) |
|---|---:|---:|---:|---:|---:|---:|
| nare turns to admitted | 9 calls (first round) | 15 | 16 | 10 | 10 | 10 |
| Input, authoring to admitted | 553,579 | 919,854 | 687,656 | 600,618 | 491,997 | 600,618 |
| Output, authoring to admitted | 30,264 | 128,168 | 117,284 | 67,481 | 79,695 | 79,695 |
| Input, authoring plus first review | 1,106,948 | 1,146,530 | 831,778 | 888,515 | 739,414 | 831,778 |
| Output, authoring plus first review | 45,133 | 139,701 | 125,052 | 85,615 | 88,542 | 88,542 |
| Ratio, authoring input | 1.00 | 1.66 | 1.24 | 1.08 | 0.89 | 1.08 |
| Ratio, authoring output | 1.00 | 4.23 | 3.88 | 2.23 | 2.63 | 2.63 |
| Ratio, authoring plus review input | 1.00 | 1.04 | 0.75 | 0.80 | 0.67 | 0.75 |
| Ratio, authoring plus review output | 1.00 | 3.10 | 2.77 | 1.90 | 1.96 | 1.96 |
| Admitted by coordinare | 44 of 47 held | yes | yes | yes | yes | 3 of 3 |
| First review | 0 of 33 accept; median 3 findings | revise, 1 finding | accept | accept | revise, 5 findings | 2 of 3 accept |
| Cache-read tokens | 0 | 0 | 0 | 0 | 0 | 0 |
| Wall time (s) | 6,191 (whole run, median) | 9,655 | 5,210 | 1,614 | 6,620 | n/a |
| Provider retries | n/a | 0 | 0 | 0 | 1 | n/a |

The baseline's first round includes a probe preflight that the nare runs omit, so the comparison favours nare slightly. Ratios use the baseline's first-round input (553,579) and first-round plus first-review input (1,106,948) and output (30,264, 45,133).

### Gap status

| Gap (spike) | Status | Evidence |
|---|---|---|
| Map-valued schemas refused (#131) | Closed | The unmodified envelope schema is accepted. All in-loop runs used it. |
| One slow or dropped request ends the run (#134) | Closed in code, partly exercised | Retries exist. a3 retried once and continued. |
| System prompt not kept on resume (#132) | Closed | The session records the system prompt. The in-loop path does not resume. |
| `${NAME}` and credential-named keys redacted (#68) | Open, not bypassed | a4r lost the tail of a streamed answer at `local_credentials`. See a4r. |
| Prose around a JSON answer (#133) | Closed | Offline: JSON followed by prose with a check set finished `done`. |
| Corrections re-driven through `--resume` | Closed for the in-loop mode | Corrections run in one session. Eight corrections ran in a4r without a resume. |
| Answer after few or no reads | Partly closed | `--require-reads` held a valid answer in a2. It counts partial reads, and it only applies after validation. |
| Citations of files never read | Open | a1 cited 13 unread files despite 35 reads. a3 cited 3. a2 cited none. The count gate does not check citations. |
| Caching (`--cache-breakpoints`) | Still open | `cache_read` was 0 on every turn of every run. |
| `check=` crashes on a tool turn with no JSON (nare#192) | Closed upstream | Confirmed by the repro in the issue. Workaround still in the spike until re-pin. |
| Cut-off replies in the Python API | Not a nare gap | The Python default for `max_cutoffs` is no correction. The CLI default is 2. Spike-side bug (a4), fixed. |

### Cost note

Every request in either harness carries the whole transcript, so moving corrections in-process does not remove that cost. The earlier note blamed resends on the resume alone. The difference between harnesses is in the number of turns and in what the model wrote (output 67k to 117k tokens in the admitted in-loop runs). Output is still the larger gap, and most of it is the model rewriting whole envelopes.

### Quality

- Two of three admitted manifests drew no finding on the divided review. That is the strongest quality signal so far, but n = 3, the reviewer had no probe evidence, and one review found five issues.
- Evidence provenance held. 35 of 35, 23 of 23 and 34 of 34 read digests matched coordinare's re-derivation from the pinned Git objects.
- Citations of unread files remain the main quality gap. a1 cited 13 unread files and coordinare admitted them, because the daemon binds every reviewed-scope input before authoring. That is coordinare's admission rule working as designed, so the gap sits in the policy as much as in nare.

### Spend

Tokens across all five runs, nare and review: input 3,389,066 and output 378,288 (3,767,354 in total). By run (input, output): a1 831,778 and 125,052; a2 888,515 and 85,615; a3 739,414 and 88,542; a4 112,328 and 3,655; a4r 817,031 and 75,424. No cache reads were reported.

### Issues

- [ViviDynamics/nare#192](https://github.com/ViviDynamics/nare/issues/192): `check=` ends the run when a tool turn has no JSON. Now closed upstream.
- Comment on [ViviDynamics/nare#68](https://github.com/ViviDynamics/nare/issues/68#issuecomment-6651532286): a4r lost the tail of a streamed answer at a credential-named key. Not reproduced offline.

### Recommendation

**The recommendation holds: do not adopt discovery authoring on nare yet.** The cost case has improved and is now plausible: authoring input is about the baseline (median 1.08 times), and authoring plus the first review is below it (median 0.75 times). Quality is mixed: two of three admitted manifests were accepted on the first pass, one drew five findings, and the baseline never accepted on its first pass. But the case is not ready for three reasons:

1. **Reliability.** Two of five runs did not admit. One was a harness bug I fixed. The other is a redaction failure on a credential-named key, which is an open nare issue. A pipeline that fails 40% of runs needs more than the cost gain.
2. **Output.** Output is still 2 to 4 times the baseline, and most of it is whole-envelope rewriting.
3. **Citations.** A manifest can cite files it never read, and the count-based read gate does not stop that. That is a coordinare policy question before it is a nare one.

What would change the answer: #68 fixed for credential-named keys, a decision on citation reads, and enough admitted runs to show the cost spread. The run cap is reached, so the next step is a decision, not another run.

### Caveats

- **n = 3 admitted, 5 total.** a1 to a3 spread from 0.89 to 1.24 times the baseline authoring input and from 0.67 to 0.80 times the authoring-plus-review input. That spread is wide enough that the median is a weak estimate.
- **Fixed scope, no probes, no installer, no execution.** As in the original runs. The baseline's per-run totals include those, so only the first-round slice is comparable.
- **Prompt gap kept on purpose.** The spike prompt still omits `GLOBAL_AUTHOR_INSTRUCTION`, so every run is refused at least once for reusing criterion ids (`scope_id_collision`). That is the spike's prompt, not nare.
- **Read threshold chosen before the runs.** 20 was about half of run 10's reads.
- **Harness bugs.** a4 was aborted by the cut-off default, now fixed.

## Re-run on nare 91e75a8 (main, 2026-11-05)

**Status: three in-loop runs on current `origin/main` (`91e75a8`, release 2026.11.44, contract 3). All three were admitted by coordinare. Two were GLM runs comparable to a1 to a3; one was claude-haiku-5-5, which is not comparable to the GLM baseline.** No nare defect was found in these runs, so nothing new was filed. The #192 workaround is removed from the harness.

### Short answer

On main, the admission problem did not recur. Three of three runs were admitted. Across the five GLM runs that were admitted (a1, a2, a3, b1, b2), authoring to admission costs a median 1.09 times the baseline input (range 0.89 to 1.94), and authoring plus the first review costs a median 0.76 times (range 0.67 to 1.16). Output stays at 2.8 times the baseline (median). First review accepts 2 of 5 manifests, against 0 of 33 baseline first reviews. The citation gap persists: 13 to 19 files cited but never read in three of the five GLM runs.

The recommendation holds for adoption (not yet), but its basis has changed. Reliability, which blocked adoption last round, no longer blocks it on main. What blocks it now is the citation-read policy and output volume, with five runs of spread that is still wide.

### Setup

- **nare.** `origin/main` at `91e75a8` (release 2026.11.44, contract 3), detached worktree `nare-spike-3`, removed at the end. The SHA is recorded in every run record as `nare_sha`.
- **Comparable settings.** Same pinned baseline (`dettmore-platform` at `bf498e8`), same reference scope and capabilities, same execution rules, same gateway, effort low, 32,768 max output, 262,144 context, a 4,000,000 token budget, 80 turns, streaming on, `--max-corrections 8`, `--require-reads 20`.
- **Harness changes for main.** The #192 workaround is removed. Contract 3 is set by nare's Python API automatically (each session records `contract` 3), so no flag is needed on this path. The CLI-only `--contract 3` flag is not used. The harness now reads its unset flags from nare's own CLI parser at run time (`cli_defaults()`): `max_cutoffs` 2, `request_retries` 3, `request_retry_seconds` 60. Each record stores the values used, so a Python-API default cannot silently differ from the CLI again. This matters: `max_cutoffs=None` in the Python API means no cut-off correction. Haiku b-run h1 hit a max-tokens cut-off, which was corrected under the new default.
- **Model.** `spark/glm-5.3-flash` for b1 and b2. `claude-haiku-5-5` for h1, through the same OpenAI-compatible gateway route.
- **Commands.** `bash .agents/state/376-nare-spike/launch-inloop.sh inloop-b1 --stream --max-corrections 8 --require-reads 20`, and the same with `SPIKE_MODEL=claude-haiku-5-5` for h1. The key is read from its file inside the command and never printed.
- **Offline smoke tests on main** (no model calls): replayed admit, refusal with correction, and read hold all behaved as on `ba35b2a`.

### Runs

| Run | Model | Outcome | nare turns | Input to end | Output to end | Review in, out | Refusals | Corrections (charged, free) | Files read | Cited (unread, partial) | First review | Wall (s) |
|---|---|---|---:|---:|---:|---|---:|---|---:|---|---|---:|
| b1 | GLM | admitted | 19 | 1,076,682 | 124,826 | 209,971, 13,763 | 3 | 6, 0 | 37 | 35 (13, 2) | revise, 3 findings | 5,616 |
| b2 | GLM | admitted | 13 | 603,577 | 85,736 | 236,221, 16,588 | 3 | 4, 0 | 22 | 40 (19, 2) | revise, 5 findings | 4,556 |
| h1 | haiku | admitted | 10 | 814,449 | 119,337 | 391,547, 12,041 | 3 | 3, 1 | 34 | 26 (1, 1) | revise, 12 findings | 479 |

The 6 corrections in b1 are three no-JSON replies, one id collision, one removed obligation and one criterion-mapping refusal. All three refusals were coordinare's: `scope_id_collision`, `obligation_removed` (enforced across checks, since one validator spans the run), and `scope_required_check_mapping`. The h1 corrections are three charged and one free. Separately, h1 had one max-tokens cut-off, corrected under the default of 2 (cut-offs are counted apart from corrections). Nothing was retried at the provider (retries 0 in every run).

### Comparison

Baseline is the median across the 47 saved runs. Run 10 is the original CLI run on `0501d80`. a1 to a3 are the earlier in-loop runs on `ba35b2a`. a4r is the failed run on `ba35b2a`. Ratios use the baseline's first-round input (553,579) and output (30,264), and first-round plus first-review input (1,106,948) and output (45,133).

| Metric | Baseline (median) | run 10 (CLI, 0501d80) | a1 | a2 | a3 | a4r (failed, ba35b2a) | b1 | b2 | h1 (haiku) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Admitted | 44 of 47 held | yes | yes | yes | yes | no | yes | yes | yes |
| Input to admission | 553,579 | 919,854 | 687,656 | 600,618 | 491,997 | n/a | 1,076,682 | 603,577 | 814,449 |
| Output to admission | 30,264 | 128,168 | 117,284 | 67,481 | 79,695 | n/a | 124,826 | 85,736 | 119,337 |
| Input, authoring plus first review | 1,106,948 | 1,146,530 | 831,778 | 888,515 | 739,414 | n/a | 1,286,653 | 839,798 | 1,205,996 |
| Output, authoring plus first review | 45,133 | 139,701 | 125,052 | 85,615 | 88,542 | n/a | 138,589 | 102,324 | 131,378 |
| Ratio, authoring input | 1.00 | 1.66 | 1.24 | 1.08 | 0.89 | n/a | 1.94 | 1.09 | 1.47 |
| Ratio, authoring output | 1.00 | 4.23 | 3.88 | 2.23 | 2.63 | n/a | 4.12 | 2.83 | 3.94 |
| Ratio, authoring plus review input | 1.00 | 1.04 | 0.75 | 0.80 | 0.67 | n/a | 1.16 | 0.76 | 1.09 |
| Ratio, authoring plus review output | 1.00 | 3.10 | 2.77 | 1.90 | 1.96 | n/a | 3.07 | 2.27 | 2.91 |
| First review | 0 of 33 accept | revise, 1 finding | accept | accept | revise, 5 findings | not run | revise, 3 findings | revise, 5 findings | revise, 12 findings |
| Cited paths never read | n/a | 9 of 34 | 13 of 36 | 0 of 15 | 3 of 22 | n/a | 13 of 35 | 19 of 40 | 1 of 26 |
| Cache-read tokens | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Wall time (s) | 6,191 (whole run, median) | 9,655 | 5,210 | 1,614 | 6,620 | 3,320 | 5,616 | 4,556 | 479 |

GLM medians over the five admitted GLM runs (a1, a2, a3, b1, b2): authoring input 1.09 times the baseline (0.89 to 1.94); authoring output 2.83 (2.23 to 4.12); authoring plus review input 0.76 (0.67 to 1.16); authoring plus review output 2.27 (1.90 to 3.07). The median first-review verdict is revise, and 2 of 5 accept.

The baseline's first round includes a probe preflight that the nare runs omit, so the comparison favours nare slightly.

### Admission outcomes

- **On main:** 3 of 3 admitted (b1, b2, h1). No redaction-caused failure. The envelope carries the `local_credentials` key the a4r answer broke on.
- **On ba35b2a:** 3 of 4 admitted (a1, a2, a3). a4r failed on the redaction of `local_credentials` (nare#68). A separate a4 run was aborted by the harness default (`max_cutoffs=None`), now fixed, and is not counted.
- **Across all in-loop runs with a valid configuration (a1 to a3, a4r, b1, b2, h1):** 6 of 7 admitted. The one failure is the #68 failure on the earlier nare, which main has fixed. Three runs on main is a small sample, so this is consistent with the fix, not proof of it.

So the admission failures from last round went away on main, for the reason we identified. The cut-off failure was the harness, not nare, and is fixed in the harness.

### Gap status

| Gap | Status on 91e75a8 | Evidence |
|---|---|---|
| Redaction of credential-named keys (#68) | Closed on main | b1, b2 and h1 admitted with the envelope key that broke a4r. The fix is the PR the coordinator confirmed offline. |
| `check=` crash on tool turns (#192) | Closed on main, workaround removed | The harness no longer patches `_check_passes`. Records carry no workaround flag. |
| Cut-off replies in the Python API | Harness fixed, not a nare gap | The Python default for `max_cutoffs` is no correction. The harness now reads the CLI default. h1 used one correction. |
| Map-valued schemas (#131) | Closed | Unchanged. The envelope schema is used as is. |
| Prose around JSON (#133) | Closed | Unchanged from the `ba35b2a` check. |
| Citations of files never read | Open | 13 to 19 unread citations in b1 and b2 (and a1). The count gate does not check citations. This is the main quality gap. |
| Caching | Still open | `cache_read` 0 in every run. |

### Cost note

Output is still the larger gap. The admitted GLM runs produced 2.3 to 4.1 times the baseline's output for authoring. Most of it is whole-envelope rewriting, which the earlier note already described.

### Quality

- First review accepted 2 of 5 GLM manifests on the divided review (a1, a2). The other three were revise, with 3 to 5 findings each. The baseline never accepted on first review (0 of 33).
- Evidence provenance held for every read: 37 of 37, 22 of 22, 34 of 34 read digests matched coordinare's re-derivation from the pinned Git objects.
- Citations of unread files remain the main quality gap, and they are a coordinare admission-policy question as much as a nare one. The daemon binds every reviewed-scope input before authoring, so an unread citation to a bound file is admitted.

### Recommendation

**The recommendation holds: do not adopt discovery authoring on nare yet, but the reason has changed.** Reliability is no longer the blocker on main: three of three runs were admitted, and the redaction failure is fixed upstream. Cost is roughly at baseline on authoring (median 1.09 times) and below it on authoring plus first review (median 0.76 times), so the case for adopting on cost is plausible. Two things still stop adoption:

1. **Citation policy.** Unread citations in four of five GLM runs. Before any adoption, decide whether a manifest may cite a file the model never read. If it may not, enforce it in the check callable and measure the cost again.
2. **Output volume and the first-review verdict.** Output is 2.3 to 4.1 times the baseline, and the first review accepts 2 of 5 runs. Five runs of GLM is still a small sample with a wide spread.

Haiku (h1) is not comparable to the GLM baseline. It was admitted on turn 10 with 1.47 times the baseline authoring input, and drew the most review findings (12). One run does not establish a model comparison.

### New nare defects

None found in these runs. The behaviours I checked are the cut-off correction, the schema refusals for `pattern` and `required` properties (which now report the expected pattern), the read hold, and the check error path. No new issue was filed. The harness's Python-API defaults were the one real defect in this round, and they are in the spike, not nare.

### Caveats

- **n = 5 GLM admitted, 1 haiku.** The spread is wide: b1 used 1.08 million input tokens to admission and b2 used 0.60 million on the same settings. The medians are weak estimates.
- **Fixed scope and no probes or execution.** As in every run in this spike. The baseline's per-run totals include those phases, so only the first-round slice is comparable.
- **Prompt gap kept on purpose.** The spike prompt still omits `GLOBAL_AUTHOR_INSTRUCTION`, so every run is refused at least once for reusing criterion ids. That is the spike's prompt, not nare.
- **Reads threshold fixed before the runs.** 20 files, as in a1 to a3.
- **Gateway.** No 502s and no non-200 responses appear in the stderr of b1, b2 or h1, and each record shows 0 provider retries.
- **Spend this round.** Input 3,332,447 and output 372,291 (nare plus review), about 3.70 million tokens. Cumulative across the spike's in-loop runs, including the earlier round: about 7.47 million tokens.

## Re-run on nare 5a68886 with cited-read holds (2026-11-07 and 2026-11-08)

**Status: four runs completed. Three were admitted and reviewed (c1c, c3, h1). One failed after eight corrections without admission (c2). Three partial runs (c1, c1r, c1b) died before finishing and are kept under their labels with no record, and are not results. The citation blocker is closed for admitted runs: in all three, every cited path had been read before the answer was admitted. The recommendation holds (not yet), for a different reason.**

### Short answer

nare now holds an answer that cites a file the run has not read, so a cited path is no longer taken on trust. In the three admitted runs, 0 of 76 cited paths were unread (earlier rounds: 13, 19, 13, 0, 3 and 1 unread). In two of them the model had already read every cited file, so the hold never fired. In h1 it fired once, with the 20-file count, and the reads cleared it.

The cost picture is unchanged in shape. The two GLM runs admitted cost 1.79 and 2.47 times the baseline authoring input, and 3.7 and 5.6 times the output. Authoring plus the first review cost 1.10 and 1.45 times the baseline input. Haiku (h1) was lower: 1.12 times for authoring and 0.87 with review, at 2.5 times the output. First review did not accept any manifest: revise, with 1, 1 and 10 findings. Reliability fell from 3 of 3 on main last round to 3 of 4 here. The failure was in repair, not in citations: c2 spent eight corrections removing or changing install and check obligations until its budget ran out.

Not yet, because output is still 2.5 to 5.6 times baseline, eight corrections were not enough for one GLM run, and no first review accepted (baseline 0 of 33). Spend is the spike's own estimate, $1.07 for the completed runs (see Spend).

### Setup

- **nare.** `origin/main` at `5a68886` (release 2026.11.64), detached worktree `nare-spike-4` (kept for re-runs), editable in the spike venv. The SHA is in every record as `nare_sha`. Changes since `91e75a8` that matter here: cited reads (#217 and #219: `require_cited_reads` and the two-argument `check`), and library runs with no explicit policy get read-only tools (#220 and #233). The spike passes an explicit Policy, so the read-only default does not apply to it. Others since the pin (fork, bash rules, MCP OAuth) are not used.
- **Pinned baseline and model.** Unchanged: `dettmore-platform` at `bf498e8`, the e2e-50 reference scope, chat-043 execution rules, `spark/glm-5.3-flash` for the GLM runs and `claude-haiku-5-5` for h1, through the same gateway. Effort low, 32,768 max output, 262,144 context, 4,000,000 token budget, 80 turns, streaming on.
- **Flags.** `--max-corrections 8`, `--require-reads 20`, `cli_defaults()` for cut-offs and retries (2, 3, 60 s). The envelope schema is coordinare's, unmodified.
- **New: the cited-read pointers.** `CITED_READ_POINTERS = ("/manifest/requirements/*/source", "/finding_responses/*/sources/*")`. These are the two path-citation fields of `_MANIFEST_ENVELOPE`, the same fields `cited_paths()` reads. `Command.cwd` and `Activation.path` name execution directories and PATH entries, not files cited for reading, so they are not held.
- **Check callable.** nare passes the run's read records as a second positional argument, so the check ingests those paths and no longer reads the session's evidence. The session is still used for usage. Each record now has `require_cited_reads`, `cited_holds` (correction count), and `admitted_final`. A run counts as admitted only if nare ended `done`, so an admission followed by a failed hold cannot be judged.
- **Offline smoke** (`SPIKE_OFFLINE=1`, replay `smoke4-cited`). A three-reply replay: the answer citing 34 unread files, reads of those 34, the same answer. The first answer was held once, with a single correction covering the 20-file count and the 34 cited paths, then admitted with `cited_unread` 0.
- **Gateway spend.** The key cannot call `/key/info`. Spend was read from the `x-litellm-key-spend` header of a one-token call before and after each run. Runs overlapped (c1c with c2, then c3 with c2 and h1), so per-run spend cannot be separated. Only the round total is reported.
- **Partial runs (not results).** c1 died when the session restarted, after two completions and with no record. c1r was stopped at 30 minutes by the background command limit (660 KB of events). c1b was stopped at 2 hours by the same limit (2.7 MB). Runs after c1b were started detached in their own session, so the limit could not stop them. Each partial directory is kept unchanged under its label. An earlier launch of c1 failed at once on an empty-array expansion under macOS bash 3.2 (fixed in the launcher) and produced no output.
- **Private state.** The launcher stays under the gitignored `.agents/state/376-nare-spike/`, as in earlier rounds, and is not committed. It reads the key from the scratchpad file inside the command.

### Runs

| Run | Model | Outcome | nare turns | Input to end | Output to end | Corrections (charged: schema, read holds) | Files read | Cited (unread, partial) | First review | Wall (s) |
|---|---|---|---:|---:|---:|---|---:|---|---|---:|
| c1c | GLM | admitted, reviewed | 19 | 990,856 | 112,208 | 4 schema, 0 | 54 | 32 (0, 7) | revise, 1 finding (`execution_definition`) | 4,825 |
| c2 | GLM | failed: `schema_violation` after 8 charged corrections, 1 cut-off | 39 | 2,522,357 | 198,596 | 8 schema, 0 | 66 | not admitted | not run | 8,504 |
| c3 | GLM | admitted, reviewed | 21 | 1,366,898 | 169,854 | 8 schema, 0 | 45 | 29 (0, 5) | revise, 1 finding (`capability`) | 5,847 |
| h1 | Haiku | admitted, reviewed | 7 | 618,184 | 75,616 | 3 schema, 1 read hold (count at 12 of 20, and 4 cited paths) | 29 | 15 (0, 1) | revise, 10 findings (coverage 3, capability 5, execution_definition 2) | 266 |

Review input and output, per admitted run: c1c 225,135 and 8,463; c3 242,937 and 7,187; h1 347,226 and 11,946. Cache reads were 0 on every turn of every run. No provider retries and no provider failures were recorded (retries 0 in every record).

The corrections, in order:
- **c1c.** An answer with no JSON document, then `scope_id_collision`, `required obligation removed`, and `scope_required_check_mapping`.
- **c2.** `scope_id_collision`, then seven `obligation_removed` refusals, then a second `scope_id_collision`. The eighth correction was spent and the ninth answer was refused, so the run ended `schema_violation`.
- **c3.** `scope_id_collision`, `required installation obligation removed`, a removed-obligation repair of 22 checks, four coverage changes, and `clean_dependency_install_only`.
- **h1.** Two `scope_id_collision`, one `scope_required_check_mapping`, then one read hold. The hold named the count gap (12 of 20 files) and four cited paths (`Gemfile.lock`, `bin/demo-magic-link`, `.rubycritic.yml`, `chart/Chart.yaml`), and was charged once.

The `scope_id_collision` refusals come from the spike's prompt, which still omits `GLOBAL_AUTHOR_INSTRUCTION` (see Caveats). They cost one or two corrections in every run.

### Comparison

Baseline medians as before: first authoring round 553,579 input and 30,264 output; with first review 1,106,948 input and 45,133 output.

| Metric | Baseline (median) | c1c | c2 | c3 | h1 (Haiku) | Previous GLM admitted (a1, a2, a3, b1, b2) |
|---|---:|---:|---:|---:|---:|---:|
| Admitted | 44 of 47 held | yes | no | yes | yes | 5 of 5 |
| Input to admission | 553,579 | 990,856 | not admitted | 1,366,898 | 618,184 | 0.89 to 1.94 times |
| Output to admission | 30,264 | 112,208 | not admitted | 169,854 | 75,616 | 2.23 to 4.12 times |
| Input, authoring plus review | 1,106,948 | 1,215,991 | n/a | 1,609,835 | 965,410 | 0.67 to 1.16 times |
| Output, authoring plus review | 45,133 | 120,671 | n/a | 177,041 | 87,562 | 1.90 to 3.07 times |
| Ratio, authoring input | 1.00 | 1.79 | n/a | 2.47 | 1.12 | as shown |
| Ratio, authoring output | 1.00 | 3.71 | n/a | 5.61 | 2.50 | as shown |
| Ratio, authoring plus review input | 1.00 | 1.10 | n/a | 1.45 | 0.87 | as shown |
| Ratio, authoring plus review output | 1.00 | 2.67 | n/a | 3.92 | 1.94 | as shown |
| First review | 0 of 33 accept | revise, 1 finding | not run | revise, 1 finding | revise, 10 findings | 2 of 5 accept |
| Cited paths never read | n/a | 0 of 32 | n/a | 0 of 29 | 0 of 15 | 13, 19, 13, 0, 3 |
| Cache-read tokens | 0 | 0 | 0 | 0 | 0 | 0 |

Input per call was about 52,000 (c1c) and 65,000 (c3) tokens. The baseline's first round includes a probe preflight that the nare runs omit, so the input comparison still favours nare slightly.

### Gap status

| Gap | Status on 5a68886 | Evidence |
|---|---|---|
| Citations of files never read (earlier: manifests cited 13 to 19 unread files) | Closed for admitted runs | 0 unread of 32, 29 and 15 cited paths. The cited hold fired once (h1) and cleared on four reads. |
| Count gate (`--require-reads 20`) | Working | Held h1 once at 12 files. Other runs passed it on their own reads. |
| Map-valued schemas (#131) | Closed | The unmodified envelope schema is used. |
| Transient provider failures (#134) | Not exercised | 0 provider failures and 0 retries in every run. c2 had one cut-off. |
| System prompt not kept on resume (#132) | Not used | The in-loop path does not resume. |
| Redaction of credential-named keys (#68) | No failure seen | No failure of that kind in these four runs. c2 failed on obligation repair, not on a redacted answer. The credential-key case was not checked separately. |
| Repair loops that remove or change obligations | Open, the main blocker now | c2 used eight corrections, seven of them about obligations (removal, scope or coverage change). c3 needed six such corrections of its eight. This is coordinare's validator refusing the repair, not a nare defect. |
| Prose around a JSON answer (#133) | Not seen this round | c1c's one non-JSON reply was not analysed further. |
| Caching (`--cache-breakpoints`) | Still open | `cache_read` was 0 on every turn. |

### Cost note

Output is still the larger gap. Most of it is whole-envelope rewriting on each correction, as in the earlier rounds. The spike's spend figure is nare's own estimate. The gateway key was shared with concurrent benchmark runs, so its delta cannot be attributed to this spike:

- **Gateway key (not attributable).** `x-litellm-key-spend` was $3.02 when the round started and $7.40 after the last run finished, a delta of $4.39. The key was shared with the nare matrix and full-tier lane, which ran at the same time and reported $2.56 of spend, plus judge calls, so the delta includes other work.
- **nare's estimate (the spike's figure).** Summing `usage.cost` (nare's own, marked estimated) over the four completed records gives $1.07 (c1c $0.20, c2 $0.48, c3 $0.29, h1 $0.10), plus reviews, which are included in those records. The partial runs' estimates are not in that sum.

### Quality

- Evidence provenance held. Every read digest matched coordinare's re-derivation from the pinned Git objects: 54 of 54 (c1c), 45 of 45 (c3), 29 of 29 (h1), and 66 of 66 for c2 (not admitted).
- Admission held the citation rule in all three admitted runs. No admitted manifest cited an unread file.
- First review: c1c and c3 drew one finding each, and h1 drew ten. None accepted outright. The baseline never accepted on first review either (0 of 33), so this is not a regression, but it is not evidence of quality either.
- c2 is the only failure. It is the same class of refusal that the spike prompt invites (obligations removed during repair), and the eight-correction budget is the limit it ran into.

### Recommendation

**Not yet. The citation blocker is closed; the blockers are now repair reliability, output volume, and first review.** Citation reads work as intended: nare holds each cited file until it is read, and no admitted manifest in this round cited an unread file. What stops adoption:

1. **Repair reliability.** One of four runs failed after eight corrections, and the GLM admitted run c3 needed eight. Haiku admitted in seven turns. Before adopting, either a higher correction budget is shown to converge, or repair moves to smaller patches than whole-envelope rewrites (caller-side work).
2. **Output volume.** Authoring output is 2.5 to 5.6 times baseline, and most of it is re-emitted envelope.
3. **First review.** None of three admitted manifests accepted outright, with 1, 1 and 10 findings.

What would change the answer: adding `GLOBAL_AUTHOR_INSTRUCTION` to the spike prompt, so the `scope_id_collision` corrections go; a repair path that does not re-emit the envelope; and more admitted runs to show the spread.

### New nare defects

None filed. The behaviours checked were the cited hold (held once, correct wording, one charge for count and cited gaps together), the two-argument check (receives the read records), and the read-only default (not in effect with an explicit Policy).

### Caveats

- **n = 3 admitted, 4 completed.** Three partials are excluded.
- **Prompt gap kept on purpose.** The spike prompt omits `GLOBAL_AUTHOR_INSTRUCTION`, so every run is refused at least once for reusing criterion ids. That is the spike's prompt, not nare.
- **Overlapping runs.** c1c with c2, and c3 with h1, ran at the same time. Wall times and per-run spend reflect that.
- **Fixed scope, no probes, no installer, no execution.** As in every run in this spike.
