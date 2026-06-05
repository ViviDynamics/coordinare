#!/usr/bin/env bash
set -u
cd "$(dirname "$0")/.."
set -a; source .env 2>/dev/null; set +a
for i in $(seq 1 60); do grep -q "IMAGE REBUILD DONE" /tmp/rebuild2.log 2>/dev/null && break; sleep 10; done
grep -q "IMAGE REBUILD DONE" /tmp/rebuild2.log 2>/dev/null || { echo "REBUILD NOT DONE"; exit 1; }
.venv/bin/python scripts/gen_fair_config.py --model gpt-oss:120b --out config.fair_persona120b.yaml | tail -1
echo "=== RE-SHAKEOUT START $(date '+%H:%M:%S') ==="
.venv/bin/python scripts/persona_bench.py \
  --repo https://github.com/ViviDynamics/conductor-bench.git \
  --prs tmp/bench_prs.json --config config.fair_persona120b.yaml \
  --backends opencode-ephemeral,codex-ephemeral \
  --judge-model spark/gpt-oss:120b --tag shakeout2-120b
echo "=== RE-SHAKEOUT DONE $(date '+%H:%M:%S') ==="
