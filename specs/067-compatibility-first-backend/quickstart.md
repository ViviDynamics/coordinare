# Quickstart — Coordinare against a self-hosted model (LM Studio)

End-to-end runbook for SC-001: move one card from `TODO` to `IN_REVIEW` against LM Studio in under 60 minutes, no source patches.

## Prereqs

- macOS or Linux with Python 3.11.
- LM Studio installed locally (download from lmstudio.ai). Tested with LM Studio 0.3.x.
- A model with **at least 32k context** loaded — `qwen3-coder-30b` (Q4_K_M) is the reference. `n_ctx >= 32768`.
- A GitHub repo on which the coordinare bot has the usual permissions (issues, PRs, project board) — see `AGENTS.md` for the standard setup.
- The performer container image already built (`./scripts/build-performer.sh`).

## Step 1 — Launch LM Studio's local server

In LM Studio's **Developer** tab:
1. Load the model.
2. Set `n_ctx = 32768`.
3. Set port `1234` (LM Studio's default).
4. Click **Start Server**. The endpoint is now `http://localhost:1234/v1`.

Verify:
```
curl -s http://localhost:1234/v1/models | jq '.data[].id'
```
You should see the loaded model id. If not, fix this before continuing — the daemon will fail noisily later, but it's cheaper to fix here.

## Step 2 — Configure coordinare

Edit `config.yaml`:

```yaml
performers:
  implementer:
    backend: opencode_compat
    base_url: http://localhost:1234/v1
    model: qwen3-coder-30b
    env:
      OPENAI_API_KEY: lm-studio    # LM Studio accepts any non-empty string
```

Repeat the `backend: opencode_compat` line under every role (`assessor`, `architect`, `implementer`, `reviewer`, `qa`, `closer`, `tech_writer`) you want routed through LM Studio. Mixed-backend configs are supported.

## Step 3 — Launch the daemon

```
set -a && source .env && set +a       # always — see CLAUDE.md
./scripts/run-coordinare.sh
```

Tail the log:
```
tail -f var/log/coordinare.log | jq .
```

## Step 4 — Pick a card

In your GitHub Project board, drop a small website card into the `TODO` column. Watch the log for `card.picked_up`. Within a few minutes you should see `env_bootstrap.complete` followed by `assessor.start`.

If `env_bootstrap` fails, the log line will include `kind: upstream_http_error` with the verbatim LM Studio body — that's where to look. The most common failure is `n_keep >= n_ctx`; the fix is to raise `n_ctx` in LM Studio.

## Step 5 — Verify acceptance

Wait for the card to reach `IN_REVIEW`. Then in the LM Studio console:

- ✅ Zero `unsupported tool type` warnings.
- ✅ Zero `developer role rewritten` warnings (the adapter remaps before send — see `research.md` R4).
- ✅ Zero `prompt_cache_key ignored` warnings.

This satisfies SC-001 and SC-002.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `status: 404, upstream_body: "model not found"` | LM Studio model id mismatch | Match `config.yaml`'s `model:` to LM Studio's exact id. |
| `status: 400, upstream_body: "n_keep ... >= n_ctx ..."` | Context too small for the prompt | Raise `n_ctx` in LM Studio to at least 32768. |
| `status: 503` followed by retry | LM Studio model warming up | Wait; coordinare's `github_retry`-style backoff will reconnect. |
| `LcdPayloadError: tool type 'web_search' not allowed` | A prompt still references hosted tools | File a bug; the prompt registry audit (FR-003) missed an entry. |

## Swap test (SC-003)

To confirm backend selection is truly orthogonal:

1. Note the current behaviour with `backend: opencode_compat` + LM Studio.
2. Flip `backend:` to `codex` and `base_url:` to `https://api.openai.com/v1`. Restart.
3. The card should pick up and complete with no other config edits.

This is the test that proves 067's success criterion: backend is a one-line flip.
