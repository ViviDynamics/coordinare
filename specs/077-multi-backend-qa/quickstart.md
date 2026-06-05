# Quickstart — 077 Diverse Multi-Backend QA Round

Operator steps to run the diverse-backend round once the implementation
(Pi backend, OpenClaw backend, opencode wiring) has landed.

## Prerequisites

1. **Creds on the host** (read-only mounts):
   - `~/.junie` (present).
   - **opencode and OpenClaw need NO login mount** — they route via
     `OPENCODE_PROVIDER_*` / `OPENCLAW_PROVIDER_*` env (each adapter writes its
     provider config at job start and resolves the key from `LITELLM_MASTER_KEY`).
     See `config.example.opencode.yaml` mode (B).
   - `LITELLM_MASTER_KEY` + the `*_PROVIDER_*` vars in `.env` (codex/junie/pi/opencode/openclaw wired).
2. **Images built**:
   - `coordinare-performer:full` (codex, claude_code, hermes, junie, opencode; the entrypoint installs pi and openclaw via npm on start).
   ```bash
   docker build -t coordinare-performer:base -f agent/performer/Dockerfile.base .
   docker build -t coordinare-performer:full --build-arg BASE_IMAGE=coordinare-performer:base -f agent/performer/Dockerfile.full agent/performer/
   ```
3. **Config**: `config.yaml` carries the diverse mapping (backup at `config.yaml.bak.*`). The final mapping is wired:
   - closer → `pi` (pi-ephemeral, `PI_PROVIDER_*`); env_bootstrap → `opencode`
     (opencode-ephemeral, `OPENCODE_PROVIDER_*`); `env_bootstrap_performer_id: opencode-ephemeral`.
   - The `opencode-ephemeral` endpoint carries the `COORDINARE_INFERENCE_*` service-inference
     env (moved off claude-ephemeral, since env_bootstrap now runs on opencode).

## Validate config before launch

```bash
set -a && source .env && set +a
.venv/bin/python -c "from coordinare import config_validation as cv; r=cv.validate_config('config.yaml'); print('passed' if r.passed else r.errors)"
```
Expect `passed`. An unknown backend name MUST surface here, not at dispatch (FR-007).

## Launch the round

```bash
set -a && source .env && set +a
nohup .venv/bin/python -m coordinare --config config.yaml > /tmp/coordinare.log 2>&1 &
curl -sf http://localhost:9090/health
```
Card #151 (in TODO) is the validating card.

## Observe per-stage backend attribution (FR-009)

```bash
# which backend container handled the current stage
docker ps --format '{{.Names}} | {{.Image}} | stage={{.Label "coordinare.performer_stage"}}'
# confirm each stage drove the shared model (no vendor fallback) — SC-002
grep -oE '"model":\s*"[^"]+"' ~/.coordinare/performer-logs/litellm-capture/*.req.json | sort | uniq -c
```

## Stage-pass check (per clarification)

A stage **passes** when its mapped backend ran, drove `spark/qwen3.6:35b` via
LiteLLM, and returned a valid terminal contract (DONE / PARTIAL_PROGRESS /
BLOCKED). The card need not merge — record model-quality issues (e.g. a weak
plan) as **findings**, not round failures.

## Produce the findings report (US5 / FR-010)

For each backend × role exercised, record: verdict (`contract-respecting` /
`needs-fix`), evidence (log events, branch artifacts, captured LiteLLM traffic),
and any follow-up. Mirror spec 076's "Phase 9 live-test fixes" format.

## Success bar (full mapping)

The round is "done" only when every mapped backend works (junie, codex,
claude_code, openclaw, hermes, opencode, pi)
(SC-001/SC-003/SC-005), each verified per the contracts in
`contracts/backend-provider-routing.md`.
