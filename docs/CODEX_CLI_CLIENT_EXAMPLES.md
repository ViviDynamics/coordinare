# Codex CLI Client Examples

Runnable client examples for integrating with Codex CLI.

- Python direct app-server control: [`docs/examples/codex_app_server_ws_client.py`](./examples/codex_app_server_ws_client.py)
- TypeScript direct app-server control: [`docs/examples/codex_app_server_ws_client.ts`](./examples/codex_app_server_ws_client.ts)
- TypeScript `exec --json` streaming (optional): [`docs/examples/codex_exec_json_client.ts`](./examples/codex_exec_json_client.ts)

## Prerequisites

- Codex CLI installed
- Authenticated account (`codex login status`)

Python app-server example prerequisite:

```bash
python -m pip install websocket-client
```

TypeScript examples use only Node built-ins plus `tsx` runner:

```bash
npx -y tsx --version
```

## Run Python App-Server Client

```bash
python docs/examples/codex_app_server_ws_client.py "Say OK and nothing else."
```

## Run TypeScript App-Server Client

```bash
npx -y tsx docs/examples/codex_app_server_ws_client.ts "Say OK and nothing else."
```

## Run TypeScript `exec --json` Client

```bash
npx -y tsx docs/examples/codex_exec_json_client.ts "Say OK and nothing else."
```

## What The App-Server Examples Cover

1. Start `codex app-server --listen ws://127.0.0.1:<random-port>`
2. Connect over WebSocket
3. Send `initialize` and `initialized`
4. Create a thread (`thread/start`, ephemeral mode)
5. Send prompt with `turn/start`
6. Stream `item/agentMessage/delta`
7. End on `turn/completed`

## Integration Notes

- Use app-server examples when you need direct turn lifecycle control and rich notifications.
- Use `exec --json` for quick, non-interactive backend workflows.
