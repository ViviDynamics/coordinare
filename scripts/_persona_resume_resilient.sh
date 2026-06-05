#!/usr/bin/env bash
# Resilient resume: re-run the persona sweep for whatever models still lack a
# committed results.json, and auto-restart if the run dies (Mac sleep, OOM,
# etc.). Per-model commits mean each restart skips completed models. Capped so a
# model that repeatedly kills the run can't loop forever.
set -u
cd "$(dirname "$0")/.."
BASE="specs/077-multi-backend-qa/persona_runs"
remaining_list() {
  : > tmp/sweep_remaining.list
  while IFS= read -r line; do
    line="${line%%$'\r'}"; [ -z "$line" ] && continue; case "$line" in \#*) continue;; esac
    tag="${line%%|*}"
    [ -f "$BASE/$tag/results.json" ] || echo "$line" >> tmp/sweep_remaining.list
  done < tmp/sweep_models.list
  grep -c '|' tmp/sweep_remaining.list 2>/dev/null || echo 0
}
for attempt in $(seq 1 10); do
  n=$(remaining_list)
  if [ "$n" -eq 0 ]; then echo "=== PERSONA SWEEP COMPLETE (all models done) $(date '+%m-%d %H:%M:%S') ==="; break; fi
  echo "=== resume attempt $attempt: $n model(s) remaining $(date '+%m-%d %H:%M:%S') ==="
  ./scripts/run_persona_sweep.sh tmp/sweep_remaining.list || echo "(run exited non-zero; will recompute + retry)"
  sleep 10
done
