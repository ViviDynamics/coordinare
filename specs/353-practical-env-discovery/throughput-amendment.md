# Throughput and reliability amendment

Status: approved in full by the maintainer on 2026-10-22, with one fresh independent review for a reused scope. The maintainer asked for these four areas: per-call telemetry, stable cacheable
prefixes, fewer format corrections and reuse of an accepted scope.

All changes must be model-agnostic. Each one helps any OpenAI-compatible or Anthropic transport, and none
depends on GLM.

## What the measurements show

Sources: Dettmore v3 (80 calls, 6578 s, held at `discovery_context_limit`), its 52 saved transcripts, and three
direct probes of the serving route on 2026-10-22.

| Measure | Value |
| --- | --- |
| Input tokens, whole attempt | 5.07 M, `cache_read` reported as 0 on every call |
| Output tokens, whole attempt | 263 K, of which about half is hidden reasoning (207 K output for 112 K visible) |
| Decode rate (probe, under load) | about 11 tokens/s |
| Prefill, identical 32 K-token prompt | 20.9 s cold, 5.3 s repeated. The server caches prefixes but does not report it. |
| Runaway call | one consolidation call used the full 32,768-token cap with a 3.5 KB reply (about 50 minutes) |
| Format corrections | 16 of 80 calls, all `triage_environment_directory`: triage marked whole directories as environment |
| Rework | v3 spent about 1.8 h reaching an accepted 129-criterion scope; v4 rebuilds the same scope on the same commit |

Output tokens dominate the cost, then uncached prefill, then corrections and rework.

## 1. Per-call telemetry

- Every model call records:
  - phase, or expected envelope;
  - wall seconds;
  - input, output and cached tokens, where the transport reports them;
  - finish reason.
- The attempt summary aggregates these per envelope: calls, seconds, input tokens, output tokens and length stops.
- The validation harness writes the per-call record into its event stream.
- No behaviour changes.

## 2. Bounded output per phase

- Each envelope gets an output cap, sent as `max_tokens` / `max_completion_tokens` to transports that accept it. The
  cap includes hidden reasoning. The caps are four times the largest valid reply observed for that envelope,
  within the configured global maximum:

  | Envelope | Cap (tokens) |
  | --- | --- |
  | `listing_triage` | 8,192 |
  | `shard_summary` | 16,384 |
  | `scope_consolidation` | 16,384 |
  | `scope_assessment` / `assessment` | 16,384 |
  | `manifest`, global part, `shard_checks` | 32,768 (unchanged) |

- A `length` stop becomes a correctable rejection. The model is asked to reason briefly and return the envelope.
  It counts against the agent's existing correction allowance, so a second runaway ends that agent as today.
- This bounds the worst case of a runaway call to minutes rather than an hour. It changes no acceptance rule.

## 3. Stable, cacheable prefixes

- **Keep the system message stable within a conversation.** Today `run_agent` changes the system message
  partway through: the schema switches from tool-only to final once the agent holds evidence. That
  invalidates the cached prefix for the whole conversation. The system message will instead carry one schema
  that allows either a tool request or the final envelope, from the first call. The "no final answer before
  holding evidence" rule moves to validation, which returns a correctable rejection.
- **Share a prefix across agents of the same role.** Content shared by every agent of a role (spine evidence,
  root listing, global part) goes in its own message ahead of the agent-specific message. The cached prefix is
  then reused across shards.
- Callers that read the agent-specific context keep reading it from one known message.

## 4. Fewer triage corrections

- An `environment` verdict on a directory is accepted when the directory is small: at most 32 files and
  1 MiB. The daemon expands it to those files, and records the expansion in the triage record.
- For a larger directory, the correction now includes that directory's file listing (at most 200 entries), so
  the model names files it can actually see instead of guessing.
- This keeps file-level triage's bound on named content while removing the 16 wasted calls.

## 5. Reuse of an accepted scope

- A new attempt on the same source commit may start from a scope contract that an earlier attempt had
  independently accepted, without rebuilding it, when all of the following hold:
  - same source commit;
  - same inventory digest;
  - same scope author protocol and policy versions;
  - the contract's acceptance record (reviewer verdict and digest) is intact.
- The new attempt gets fresh budgets for construction and execution only. Its diagnostics record
  `scope_reused_from` with the earlier attempt's identity.
- Any later scope reopen (new source evidence during construction) still goes through independent review, as
  today.
- **Production:** `restart_environment_attempt` carries the accepted contract into the new journal.
- **Validation harness:** a flag `--reuse-scope-from <prior output>` reads the earlier journal.

**Maintainer ruling:** a reused contract is not accepted as it stands. One fresh independent review must accept it
before construction starts. That review costs about 1 to 10 calls, against about 80 to rebuild the scope, and it
keeps an independent acceptance inside every attempt.

## Order of delivery

1, then 2 and 4, then 3, then 5. Each step ships with tests and gets one independent review, then preflight and
push to #355. The current Dettmore v4 run continues on its own commit and is not affected.
