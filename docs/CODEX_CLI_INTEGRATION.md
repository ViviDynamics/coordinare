# Codex CLI Integration Guide

This document explains how to integrate an application with Codex CLI, including direct local server usage for observability and control.

Validated against local `codex-cli 0.104.0` (`codex --version`).

## 1) What Codex CLI can do

Core command groups:

- Interactive agent UX: `codex` (TUI), `codex resume`, `codex fork`
- Non-interactive automation: `codex exec`, `codex review`
- Programmatic/server integration: `codex app-server` (experimental), `codex mcp-server`
- MCP server management: `codex mcp add|get|list|remove|login|logout`
- Auth/session management: `codex login`, `codex logout`
- Diff/task workflows: `codex apply`, `codex cloud` (experimental)
- Operational tooling: `codex sandbox`, `codex debug`, `codex features`, `codex completion`

Global runtime controls you can set per invocation:

- Model/profile: `--model`, `--profile`
- Sandboxing: `--sandbox read-only|workspace-write|danger-full-access`
- Approval policy: `--ask-for-approval untrusted|on-failure|on-request|never`
- Config override: `-c key=value`
- CWD override: `--cd <dir>`
- Multi-dir write access: `--add-dir <dir>`
- Optional web search tool: `--search`

## 2) Recommended integration modes

### Mode A: Simple subprocess integration (`codex exec --json`)

Use this when you want minimal complexity and can stream JSON Lines from stdout.

Example:

```bash
codex exec --json --skip-git-repo-check "Return exactly: OK"
```

Observed JSONL event shape (example run):

```json
{"type":"thread.started","thread_id":"..."}
{"type":"turn.started"}
{"type":"item.completed","item":{"id":"item_0","type":"reasoning","text":"..."}}
{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"OK"}}
{"type":"turn.completed","usage":{"input_tokens":...,"output_tokens":...}}
```

Useful flags for backend services:

- `--json`: machine-readable event stream
- `--output-last-message <file>`: write final assistant message to a file
- `--ephemeral`: do not persist session files
- `--output-schema <schema.json>`: constrain final output shape
- `--skip-git-repo-check`: run outside git repos

### Mode B: Full protocol integration (`codex app-server`)

Use this when you need direct control, bidirectional messaging, and fine-grained observability.

`app-server` supports:

- `stdio://` transport (default)
- `ws://IP:PORT` transport for local WebSocket clients

Start a local server:

```bash
codex app-server --listen ws://127.0.0.1:4040
```

Current startup output:

```text
codex app-server (WebSockets)
  listening on: ws://127.0.0.1:4040
  note: binds localhost only (use SSH port-forwarding for remote access)
```

Generate protocol artifacts for strong typing:

```bash
codex app-server generate-json-schema --out ./tmp/codex-schema
codex app-server generate-ts --out ./tmp/codex-ts
```

The protocol is JSON-RPC style (`request`, `response`, `notification`) and includes typed client requests + server notifications.

### Mode C: Expose Codex as an MCP server (`codex mcp-server`)

Use this when your host application is already MCP-native and you want Codex available as a tool endpoint over stdio.

```bash
codex mcp-server
```

Also relevant: `codex mcp add|get|list|remove` to wire other MCP servers into Codex sessions.

## 3) Local server details: driving Codex directly

When using `app-server`, typical request sequence is:

1. Send `initialize` request with client info.
2. Send `initialized` notification.
3. Create/open a thread (`thread/start`, `thread/resume`, etc.).
4. Start a turn (`turn/start`) with user input.
5. Consume streaming notifications until `turn/completed`.

Common request methods (from generated bindings):

- `initialize`
- `thread/start`, `thread/resume`, `thread/fork`, `thread/list`, `thread/read`
- `turn/start`, `turn/steer`, `turn/interrupt`
- `review/start`
- `model/list`

High-value notifications for observability:

- `turn/started`, `turn/completed`
- `item/agentMessage/delta` (token/text stream)
- `item/commandExecution/outputDelta` (shell output stream)
- `item/fileChange/outputDelta` (file patch/change stream)
- `turn/diff/updated` (latest unified diff for current turn)
- `thread/tokenUsage/updated`
- `error`

Server-initiated requests your client should handle:

- `item/commandExecution/requestApproval`
- `item/fileChange/requestApproval`
- `item/tool/requestUserInput`
- `item/tool/call`

If your app ignores these requests, turns can stall or fail when user approval/input is needed.

## 4) Minimal WebSocket JSON-RPC example

```json
{"id":1,"method":"initialize","params":{"clientInfo":{"name":"my-app","version":"0.1.0"}}}
{"method":"initialized"}
{"id":2,"method":"thread/start","params":{"cwd":"/absolute/workdir","sandbox":"workspace-write","approvalPolicy":"on-request"}}
{"id":3,"method":"turn/start","params":{"threadId":"<thread-id>","input":[{"type":"text","text":"Summarize this repository."}]}}
```

Then listen for notifications like `item/agentMessage/delta` and `turn/completed`.

## 5) Security and production guidance

- Prefer `workspace-write` sandbox over `danger-full-access`.
- Prefer `on-request` approvals for interactive tools; use `never` only in tightly controlled automation.
- Keep `app-server` bound to localhost (`127.0.0.1`) and expose remotely only through authenticated tunnels.
- Treat generated protocol artifacts as versioned contracts and regenerate on CLI upgrades.

## 6) Practical implementation advice for this repository

Given `agent/performer/src/performer/backends/codex.py` is currently a stub, the fastest path is:

1. Implement subprocess streaming with `codex exec --json` first.
2. Normalize events into your internal performer event model.
3. Add `app-server` integration if you need approvals, turn steering (`turn/steer`), or richer telemetry.

This staged approach gets a working backend quickly while preserving a clear migration path to full protocol control.
