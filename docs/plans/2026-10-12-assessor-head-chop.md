# Assessor output captured as head-chopped suffix: BACKEND_FORMAT_ERROR loops 3x, card parks in Blocked

Issue #83

## Scope
In:
1. **Result-event seam (root-cause-adjacent fix)**: `ClaudeCodeBackend._handle_event` joins
   the final output from `_output_accumulator` only. The CLI's `result` event carries an
   authoritative `result` field with the complete final message text, immune to streamed
   chunk loss in the accumulator. Prefer it when present; keep the accumulator as fallback
   (older CLIs / proxies that strip the field); log a divergence event when both exist and
   differ, so the capture seam becomes visible.
2. **Parse-failure evidence (durable observability)**: `_handle_backend_parse_failure`
   emits a structured `backend.parse_failure.raw_shape` log on EVERY parse failure (not
   just retries exhausted): captured length, `has_open_brace`, redacted head and tail
   (50 chars each). Performer stderr is reaped with the container, so this daemon-visible
   log is the only durable evidence for the capture seam.

Out:
- **Head-repair in `_extract_json`** (issue direction 1): rejected on analysis. The
  observed fragment (`ufficient": true, ...}`) lost `{"s` — a bare `{` prepend yields
  invalid JSON, and a `{"` prepend yields valid JSON with a corrupted key
  (`ufficient: true`), silently destroying the `sufficient` semantic. Repair only helps a
  narrower loss mode than observed while adding a corruption path.
- **Stream-delta accumulation rewrite**: pinning which CLI event type drops the head chunk
  needs a live stream transcript; the evidence log ships in this PR to make the next
  occurrence conclusive.
- **CLI version pinning** (entrypoint installs unpinned 2.1.296): separate ops decision.

## Assumptions
- The claude CLI `result` event carries the full final message in its `result` field
  (stream-json shape: `{"type": "result", "subtype": "success", "result": "<final text>"}`).
  The fallback keeps older/proxied shapes safe, and the divergence log surfaces any
  discrepancy for free.
- The performer's plain (non-proxied) dispatch path is what failed in the incident;
  the DualModelProxy result assembly (080) is untouched by this PR.
- Redaction reuse (`_redact_secrets`) applies to any logged raw-output slices.

## Tasks
- [x] 1. Backend result-event preference: prefer `result` field over accumulator join,
      fallback when absent, divergence log when both exist and differ. Failing test:
      backend fed assistant text missing its head chunk plus a `result` event carrying the
      complete text must produce `status.output ==` complete text.
- [x] 2. Parse-failure raw-shape evidence log on every `_handle_backend_parse_failure`
      invocation. Failing test: parse-failure path emits `backend.parse_failure.raw_shape`
      with length and redacted head/tail.
- [x] 3. House-rules sweep + preflight; open PR with `Closes #83`.
