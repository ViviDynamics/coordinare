#!/usr/bin/env bash
# Periodically consolidate the live matrix output into the committed snapshot
# files and commit, so a crash never loses completed-model results. Exits once
# the matrix drivers are gone. Commits ONLY the two result files (narrow scope).
set -u
cd "$(dirname "$0")/.."
while : ; do
  .venv/bin/python scripts/bench_consolidate.py >/dev/null 2>&1 || true
  if ! git diff --quiet -- specs/077-multi-backend-qa/matrix_results.json 2>/dev/null; then
    git add specs/077-multi-backend-qa/matrix_results.json specs/077-multi-backend-qa/matrix_results.md 2>/dev/null
    git commit -q -m "chore(077): matrix snapshot [auto]" 2>/dev/null || true
  fi
  pgrep -f "run_model_matrices.sh\|_phase2_newmodels.sh" >/dev/null || { 
    .venv/bin/python scripts/bench_consolidate.py >/dev/null 2>&1 || true
    git add specs/077-multi-backend-qa/matrix_results.* 2>/dev/null
    git commit -q -m "chore(077): matrix snapshot [auto, final]" 2>/dev/null || true
    break; }
  sleep 600
done
