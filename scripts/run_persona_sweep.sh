#!/usr/bin/env bash
# 077: full persona benchmark sweep — persona_bench across a model list, one
# model at a time (Spark single-request), all backends, judge on. Writes durable
# committed results per model (specs/077-multi-backend-qa/persona_runs/<tag>/)
# and commits after each model so a crash never loses a completed model.
set -u
cd "$(dirname "$0")/.."
set -a; source .env 2>/dev/null; set +a
LIST="${1:?usage: run_persona_sweep.sh <models.list>}"
REPO="https://github.com/ViviDynamics/conductor-bench.git"
JUDGE="spark/gpt-oss:120b"
while IFS= read -r line; do
  [ -z "$line" ] && continue; case "$line" in \#*) continue;; esac
  tag="${line%%|*}"; model="${line#*|}"
  echo "############################################################"
  echo "### PERSONA SWEEP: $tag ($model)  $(date '+%m-%d %H:%M:%S')"
  echo "############################################################"
  .venv/bin/python scripts/gen_fair_config.py --model "$model" --out "config.fair_${tag}.yaml" | tail -1
  .venv/bin/python scripts/persona_bench.py \
    --repo "$REPO" --prs tmp/bench_prs.json \
    --config "config.fair_${tag}.yaml" \
    --judge-model "$JUDGE" --tag "$tag" 2>&1 | tail -25
  git add "specs/077-multi-backend-qa/persona_runs/$tag" 2>/dev/null
  git commit -q -m "chore(077): persona sweep results — $tag [auto]" 2>/dev/null || true
done < "$LIST"
echo "=== PERSONA SWEEP COMPLETE $(date '+%m-%d %H:%M:%S') ==="
