# Quickstart: your first card, with your own models

From clone to a first dispatched card. Target: under thirty minutes on a laptop
with [Ollama](https://ollama.com).

Coordinare runs AI agents against your repository with a GitHub token that can
write to it. Before you point it at anything you care about, read
[the threat model](security/threat-model.md). The short version is at the end of
this page.

## 1. Install

```bash
git clone https://github.com/ViviDynamics/coordinare.git
cd coordinare
uv sync --extra dev
```

## 2. Get a model answering

Any of these works. Pick the one you already have.

**Ollama, locally.** The simplest, and free.

```bash
ollama pull qwen2.5-coder:32b
ollama list                      # note the exact name; you will need it verbatim
```

**An OpenAI-compatible endpoint** (vLLM, LM Studio, your own LiteLLM gateway, a
hosted provider). You need its base URL and one model name it serves.

**The Anthropic API.** You need a key. Note this sends your repository's code to
Anthropic; if that is not acceptable for your codebase, use one of the first two
against an endpoint you control.

## 3. Create a GitHub token and a project board

Coordinare reads cards from a GitHub Project (V2) and writes branches and pull
requests.

1. Create a Project and note its number (it is in the URL).
2. Create a **fine-grained** personal access token scoped to the single
   repository coordinare will work on, plus that project.

Which permissions? [The threat model has the exact list per feature](security/threat-model.md#github-token-permissions).
Two are easy to miss: **Issues needs write** (coordinare posts clarifying
questions and manages labels), and **Administration needs read** (branch
protection lookups). Granting too little here is the most common cause of a run
failing halfway.

## 4. Configure

Copy the preset matching your choice in step 2:

```bash
cp config.example.ollama.yaml config.yaml              # or
cp config.example.openai-compatible.yaml config.yaml   # or
cp config.example.anthropic.yaml config.yaml
```

Edit every line marked `CHANGE ME`. There are only a few, and they are all near
the top.

Put your secrets in `.env`, never in `config.yaml`:

```bash
cp .env.example .env
# COORDINARE_GITHUB_TOKEN=github_pat_...
# ANTHROPIC_API_KEY=...          (only for the Anthropic preset)
```

## 5. Check it before you run it

Two commands, in this order. The first proves the file parses; the second proves
the world it describes actually exists.

```bash
set -a && source .env && set +a

.venv/bin/python -m coordinare --config config.yaml config validate
.venv/bin/python -m coordinare --config config.yaml doctor
```

`doctor` tells you what is wrong and what to change. A model-name typo, for
instance, reports the names your endpoint actually serves:

```
  [FAIL] model 'coder'
         'qwen2.5-coder:32b' is not served by 'ollama-local'
         fix: pull it with `ollama pull qwen2.5-coder:32b`, or set the model to
              one this endpoint already serves: llama3.2:3b, gpt-oss:20b
```

Fix anything it reports before continuing. That is the entire point of running
it: these failures are much cheaper here than halfway through a card.

## 6. Run it

```bash
bin/run-coordinare
```

Put a card in your project's TODO column with a clear title and acceptance
criteria. Coordinare picks it up on the next poll, dispatches it, and moves it
along. Watch at <http://127.0.0.1:8090>.

## What you just configured, and what you did not

The presets configure **two** roles: `implementer` writes the code and opens the
pull request, `reviewer` reads it back. That is enough for a card to go end to
end.

Coordinare has nine roles. The other seven are listed, commented out, at the
bottom of each preset. Add them once the basic loop works, rather than
provisioning nine before you have seen one card succeed.

| Role | What it adds | Needed first? |
|---|---|---|
| `implementer` | Writes code, opens the PR | **Yes** |
| `reviewer` | Reviews the PR | **Yes** |
| `assessor` | Checks a card has enough context before work starts | No |
| `architect` | Commits a plan before implementation | No |
| `security` | Runs the security scan gate | No |
| `qa` | Validates acceptance criteria | No |
| `tech_writer` | Maintains `docs/wiki/` | No |
| `closer` | Final pass before human review | No |
| `advocate` | Answers inbound issues from the documentation | No |
| `curator` | Proposes ready issues to the board backlog | No |

Unconfigured roles are skipped, not failed.

## Notifications are optional

You do not need Slack, or email, or anything else. Coordinare runs with no channels
configured, and that is a supported setup rather than a degraded one.

**A channel you have half-configured will not stop coordinare starting.** If you add a
Slack channel and have not pasted the webhook URL yet, that channel is inactive and
coordinare runs without it, saying so once:

```
notification_channel_skipped  channel=team-slack  reason=webhook_url: Field required
```

This is deliberate. Notifications are not essential to coordinare's work — without
them it does the job and tells nobody, which is recoverable. Broken GitHub or model
configuration is different: coordinare would do the *wrong* thing while looking
healthy, so those still refuse to start.

`coordinare config validate` still reports a half-configured channel as an error. The
daemon tolerates it; the tool for finding configuration problems keeps finding it.

**Where stall warnings appear when nothing is configured.** Coordinare logs one line at
startup telling you what will and will not reach you:

```
notification_posture  summary="no notification channels configured; stall signals
appear in the coordinare log and the dashboard activity feed only"
```

A card that stops making progress is written to the log at warning level whatever else
you have turned on:

```
card_stuck  card_id=...  stage=implementing  stuck_minutes=45
```

So the honest minimum is: watch the log. The dashboard activity feed shows the same
thing more conveniently, and channels push it to you, but neither is required.

## Before you point this at anything important

- **Only humans approve pull requests.** Coordinare will not merge on its own
  say-so, and that is the main thing standing between a prompt-injected model and
  your `main` branch. Read the diff, not the summary.
- **The dashboard has no authentication** and is loopback-only by design. If you
  bind it elsewhere, you have put an unauthenticated control plane on a network.
- **Public issue and comment text reaches model prompts.** On a public repository
  that text is written by strangers.

All three are covered properly in [the threat model](security/threat-model.md).

## When something goes wrong

Run `doctor` first; most setup problems are one of its checks. Beyond that,
`bin/performer-logs` shows what an agent actually did, and the dashboard's
activity feed distinguishes "working" from "wedged".
