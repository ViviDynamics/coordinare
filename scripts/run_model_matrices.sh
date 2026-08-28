#!/usr/bin/env bash
# 077: run the browser-control diagnostic matrix across a list of models, one at
# a time (the model host is single-request). Reads "tag|model" lines from the file given
# as $1 (lines starting with # are skipped). Archives each model's screenshots +
# log under tmp/model_runs/<tag>/.
set -u
cd "$(dirname "$0")/.."
set -a; source .env 2>/dev/null; set +a

LIST="${1:?usage: run_model_matrices.sh <models.list>}"
mkdir -p tmp/model_runs

while IFS= read -r line; do
  [ -z "$line" ] && continue
  case "$line" in \#*) continue;; esac
  tag="${line%%|*}"; model="${line#*|}"
  out="tmp/model_runs/$tag"; cfg="config.fair_${tag}.yaml"
  echo "############################################################"
  echo "### $tag  ($model)   $(date '+%H:%M:%S')"
  echo "############################################################"
  .venv/bin/python scripts/gen_fair_config.py --model "$model" --out "$cfg" | tail -1
  rm -rf "$out"; mkdir -p "$out"
  rm -f tmp/smoke_shots/*
  .venv/bin/python scripts/smoke_browser.py --config "$cfg" 2>&1 | tee "$out/matrix.log"
  cp tmp/smoke_shots/* "$out/" 2>/dev/null
  echo "--- file types ($tag) ---" | tee -a "$out/matrix.log"
  for f in "$out"/*.png; do [ -f "$f" ] && echo "$(basename "$f"): $(file -b "$f" | cut -d, -f1)" | tee -a "$out/matrix.log"; done
done < "$LIST"
echo "=== ALL MODEL MATRICES DONE $(date '+%H:%M:%S') ==="
