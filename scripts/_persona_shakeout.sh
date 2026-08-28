#!/usr/bin/env bash
# Wait for the image rebuild, then run a fast persona-bench shakeout:
# all 9 personas x 2 representative backends (opencode=Ollama-direct,
# codex=LiteLLM) on gpt-oss:120b, judge on. Validates every grader + both
# routing paths + the judge before the full multi-model sweep.
set -u
cd "$(dirname "$0")/.."
set -a; source .env 2>/dev/null; set +a
echo "waiting for image rebuild..."
for i in $(seq 1 60); do
  grep -q "IMAGE REBUILD DONE" /tmp/rebuild.log 2>/dev/null && break
  sleep 10
done
grep -q "IMAGE REBUILD DONE" /tmp/rebuild.log 2>/dev/null || { echo "REBUILD NOT DONE — aborting"; exit 1; }
echo "rebuild done; generating fair config for gpt-oss:120b"
.venv/bin/python scripts/gen_fair_config.py --model gpt-oss:120b --out config.fair_persona120b.yaml | tail -1
echo "=== persona shakeout START $(date '+%H:%M:%S') ==="
.venv/bin/python scripts/persona_bench.py \
  --repo https://github.com/ViviDynamics/conductor-bench.git \
  --prs tmp/bench_prs.json \
  --config config.fair_persona120b.yaml \
  --backends opencode-ephemeral,codex-ephemeral \
  --judge-model local/gpt-oss:120b \
  --tag shakeout-120b
echo "=== persona shakeout DONE $(date '+%H:%M:%S') ==="
