#!/usr/bin/env bash
# Phase 1: backfill failure-cell logs for the 3 already-committed models
# (re-run only their non-PASS cells under the log-capturing code).
# Phase 2: resume the sweep for the remaining models (4..24), which capture
# logs natively. Commits after each model — crash-safe.
set -u
cd "$(dirname "$0")/.."
set -a; source .env 2>/dev/null; set +a
REPO="https://github.com/ViviDynamics/conductor-bench.git"; JUDGE="spark/gpt-oss:120b"
for entry in "gpt-oss-120b|gpt-oss:120b" "gpt-oss-20b|gpt-oss:20b" "qwen36-35b|qwen3.6:35b"; do
  tag="${entry%%|*}"; model="${entry#*|}"
  echo "############## BACKFILL FAILURES: $tag ($model) $(date '+%m-%d %H:%M:%S') ##############"
  .venv/bin/python scripts/gen_fair_config.py --model "$model" --out "config.fair_${tag}.yaml" | tail -1
  .venv/bin/python scripts/persona_bench.py --repo "$REPO" --prs tmp/bench_prs.json \
    --config "config.fair_${tag}.yaml" --judge-model "$JUDGE" --tag "$tag" --rerun-failures 2>&1 | tail -20
  git add "specs/077-multi-backend-qa/persona_runs/$tag" 2>/dev/null
  git commit -q -m "chore(077): backfill failure logs — $tag [auto]" 2>/dev/null || true
done
echo "############## BACKFILL DONE — resuming sweep $(date '+%m-%d %H:%M:%S') ##############"
exec ./scripts/run_persona_sweep.sh tmp/sweep_resume.list
