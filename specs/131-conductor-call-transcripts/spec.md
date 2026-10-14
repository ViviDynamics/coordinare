# 131: Transcripts for the conductor's own model calls

Issue #131 (P2, part of #116). Depends on #129 (run transcripts), #130 (run header),
#133 (viewer), #134 (token ledger).

## Problem

The conductor makes model calls of its own: assessing a card, classifying scope, classifying
human feedback, classifying a comment, the config assistant, env-manifest and env-signature
inference, and the observer. None of them were saved, so an operator could not see what the
model was asked or why the conductor decided what it did.

## Call inventory (before this change)

Every model call the daemon makes, with its position at the start of this spec.

| Kind | Call site (file:line) | Path to the model | Card |
| --- | --- | --- | --- |
| `assess` | `graph/nodes/assess_card.py:100` (`backend.assess`) | `AnthropicApiBackend.assess` / `OpenAiApiBackend.assess` / CLI backends (`services/conducting.py`) | yes |
| `assess_questions` | `graph/nodes/handle_blocked.py:211` (`backend.assess`) | same | yes |
| `feedback_classify` | `graph/nodes/classify_human_feedback.py:203` (`_classify_with_ai`, via `conducting_backend.prompt`) | same | yes (`current_card`) |
| `persona_scope` | `services/persona_classifier.py:598` (`conducting_backend.prompt`, under `asyncio.wait_for`); reached from `graph/nodes/classify_scope.py` | same | yes |
| `config_assistant` | `services/config_assistant.py:432` (`backend.prompt`) | same | no (`system`) |
| `env_manifest` | `services/env_manifest_llm.py:102` (`chat_json`, bound to `OpenAiApiBackend.chat_json` from `daemon.py:3758`, called at `daemon.py:4996`) | `OpenAiApiBackend._chat` | no (`system`) |
| `env_signature` | `services/env_signature.py:177` (`backend.prompt`) | same | no (`system`) |
| `comment_classify` | `services/issue_comment_service.py:147` (`conducting_backend.prompt`) | same | no (`system`) |
| `pr_comment_classify` | `services/pr_conversation_feedback.py:76` (`backend.prompt`) | same | no (`system`) |
| `observer` | `services/observer.py:340` (`backend.prompt`, under `asyncio.wait_for`); backend built at `graph/nodes/monitor/body.py:628` and `:3907` | same | yes (`query.card_id`) |

The model clients themselves (the only places that reach a model):

- `services/claude.py:184` and `:266`: `self._client.messages.create` (Anthropic SDK).
- `services/conducting.py:291`: `httpx` POST to `{base_url}/chat/completions` (`OpenAiApiBackend._chat`).
- `services/conducting.py:377`, `:411`, `:462`, `:497`, `:565`: subprocess model CLIs (`claude --print`, `opencode run`, `codex exec`).

Not conductor calls, and left alone: `bench/judge.py:59` and `eval/gateway.py:73` (the bench
and eval harnesses, run outside the daemon), and `daemon.py:1296` and `:4074` (docker).

## Design

One wrapper, `services/conductor_calls.py`, owns every conductor model call.

- **Open a call.** Call sites write `async with conductor_call(source, kind=..., card_id=...,
  symphony=...) as call:` around the model call and its parsing, then `call.decide(text)`.
  `source` is the backend (its `calls` attribute) or the log itself. A backend without a log
  (test doubles, `NullBackend`) records nothing.
- **Every model send goes through `ConductorCallLog.invoke`.** The model clients build a
  `ModelRequest` (model, endpoint name, system, prompt, parameters) and pass a `send` closure
  that returns a `ModelReply` (text, tool calls, usage, error, timed out). A call made with no
  open call is recorded as kind `unattributed` under `system`, so nothing escapes the record.
- **Record shape.** One file per call, in the #129 format: the first line is a `run_header`
  (the #130 header) with a `conductor` section (`kind`, `symphony`, `card_id`, `decision`,
  `parameters`), `prompt.rendered` and `prompt.system`. Then `assistant_text`, `tool_call`,
  `usage` (scope `message`, carries `stage: conductor:<kind>`), `error` and a closing `result`
  whose text is the decision (`is_error` set on failure).
- **Path.** `<transcripts root>/conductor/<card id or "system">/<call_id>.jsonl`.
  The issue wrote `_conductor`. The writer's path sanitiser strips leading underscores, so the
  folder is `conductor`. Config refuses a symphony named `conductor`, so the folder is never
  shared with a symphony.
- **Written at close, off the loop.** The file is written once, when the call's `async with`
  exits (normal exit, error or cancellation). The write runs on a worker thread as a tracked
  task, so neither the call nor the decision waits for disk. A failure is logged and dropped.
  A process killed mid-call loses that one call's file.
- **Ledger.** The usage record is counted by `TokenLedger` under the card (`card` = the card id
  or `system`) and stage `conductor:<kind>`, priced by the same `cost_tracking` table. Its
  symphony folder is `conductor`, so the `token_usage` block gains a `conductor` bucket. The
  named symphonies, `unattributed` and `conductor` together add up to the total. The spend
  strip does not draw that bucket yet. The boot rebuild reads the same folder name, so live
  and rebuilt totals agree.
- **Limits.** The CLI backends report no token usage, so their calls have no `usage` record
  and count zero tokens. The record says why.
- **Redaction and safety.** Every record goes through the writer, which applies
  `transcript_redaction` and the exact-secret values, and the writer's safety rules
  (daemon-only root, no links, `0700`/`0600`, byte cap). `transcripts.enabled: false` makes the
  log a no-op: no files, no ledger, and the model call is unchanged.

## Viewer and card detail

- `GET /api/card-runs/{card}` already lists every symphony folder for a card, so conductor
  files appear with no route change. `run_viewer._header_summary` reads the `conductor`
  section: its stage is `conductor:<kind>` and it carries the decision.
- The card detail's run list (`dashboard_static/runs.js`) labels a conductor call with its kind
  and decision, and links it to `/runs/conductor/<card>/<call_id>`, which the existing viewer
  opens.
- The activity feed has no assessment or classification rows today, so nothing there links to
  a call. The timeline link is #147's.

## Acceptance

- Every call site in the table goes through `conductor_call`, and the guard test
  (`tests/unit/test_131_conductor_guard.py`) fails if a new direct model client call appears
  outside the wrapper.
- A card detail lists its conductor calls next to its performer runs, and the link opens the
  viewer (`tests/e2e/test_131_conductor_calls.py`, Chromium and Firefox).
