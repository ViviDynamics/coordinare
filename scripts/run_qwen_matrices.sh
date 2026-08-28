#!/usr/bin/env bash
# 077: run the browser-control diagnostic matrix across Qwen variants,
# one model at a time (the model host is single-request). Archives each model's
# screenshots + log under tmp/qwen_runs/<tag>/.
set -u
cd "$(dirname "$0")/.."
set -a; source .env 2>/dev/null; set +a

# tag|model  (model name as the model host/LiteLLM expects, pre-prefix; gen handles local/)
RUNS=(
  "q36-35b|qwen3.6:35b"
  "q3coder-30b|qwen3-coder:30b"
  "q25-32b|qwen2.5:32b"
  "q25coder-14b|qwen2.5-coder:14b-instruct-q6_K"
)

mkdir -p tmp/qwen_runs
for entry in "${RUNS[@]}"; do
  tag="${entry%%|*}"; model="${entry#*|}"
  out="tmp/qwen_runs/$tag"
  cfg="config.fair_${tag}.yaml"
  echo "############################################################"
  echo "### $tag  ($model)   $(date '+%H:%M:%S')"
  echo "############################################################"
  .venv/bin/python scripts/gen_fair_config.py --model "$model" --out "$cfg" | tail -1
  rm -rf "$out"; mkdir -p "$out"
  rm -f tmp/smoke_shots/*
  .venv/bin/python scripts/smoke_browser.py --config "$cfg" 2>&1 | tee "$out/matrix.log"
  # archive proofs + per-backend file types
  cp tmp/smoke_shots/* "$out/" 2>/dev/null
  echo "--- file types ($tag) ---" | tee -a "$out/matrix.log"
  for f in "$out"/*.png; do [ -f "$f" ] && echo "$(basename "$f"): $(file -b "$f" | cut -d, -f1)" | tee -a "$out/matrix.log"; done
done
echo "=== ALL QWEN MATRICES DONE $(date '+%H:%M:%S') ==="
