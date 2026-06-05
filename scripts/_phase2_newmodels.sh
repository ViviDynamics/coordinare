#!/usr/bin/env bash
set -u
cd "$(dirname "$0")/.."
# wait for the 14-model matrix run AND the pull job to finish
while pgrep -f run_model_matrices.sh >/dev/null || pgrep -f pull_spark_models.sh >/dev/null; do
  sleep 60
done
echo "=== phase1 (14 models) + pulls complete; running 4 new models $(date '+%H:%M:%S') ==="
./scripts/run_model_matrices.sh tmp/new_models.list
