# OpenCode SDK & API Documentation

## Overview

OpenCode is an open-source AI coding agent for the terminal with support for 75+ LLM providers. This document covers the SDK and HTTP API for programmatic interaction.

---

## Installation

### OpenCode CLI

```bash
# curl installer (recommended)
curl -fsSL https://opencode.ai/install | bash

# Or via package managers
# npm: npm install -g opencode-ai
# brew: brew install opencode-ai
```

### SDK Package

```bash
npm install @opencode-ai/sdk
```

---

## SDK Usage (JavaScript/TypeScript)

### Create Client

```javascript
import { createOpencode } from "@opencode-ai/sdk"

const { client } = await createOpencode()
```

#### Options

| Option | Type | Description | Default |
|--------|------|-------------|---------|
| `hostname` | `string` | Server hostname | `127.0.0.1` |
| `port` | `number` | Server port | `4096` |
| `signal` | `AbortSignal` | Abort signal for cancellation | `undefined` |
| `timeout` | `number` | Timeout in ms for server start | `5000` |
| `config` | `Config` | Configuration object | `{}` |

---

## HTTP API Server

### Starting the Server

```bash
opencode serve [--port <number>] [--hostname <string>] [--cors <origin>]
```

#### Options

| Flag | Description | Default |
|------|-------------|---------|
| `--port` | Port to listen on | `4096` |
| `--hostname` | Hostname to listen on | `127.0.0.1` |
| `--mdns` | Enable mDNS discovery | `false` |
| `--mdns-domain` | Custom domain for mDNS | `opencode.local` |
| `--cors` | Additional browser origins | `[]` |

### Authentication

```bash
# Set password (applies to opencode serve and opencode web)
OPENCODE_SERVER_PASSWORD=your-password opencode serve

# Optional: customize username
OPENCODE_SERVER_USERNAME=admin opencode serve
```

Credentials are stored in `~/.local/share/opencode/auth.json`.

### OpenAPI Spec

View the full OpenAPI 3.1 spec at:

```
http://<hostname>:<port>/doc
```

---

## API Endpoints Reference

### Global

| Method | Path | Description |
|--------|------|-------------|
| GET | `/global/health` | Server health & version |
| GET | `/global/event` | Global events (SSE stream) |

### Sessions

| Method | Path | Description |
|--------|------|-------------|
| GET | `/session` | List all sessions |
| POST | `/session` | Create new session |
| GET | `/session/:id` | Get session details |
| DELETE | `/session/:id` | Delete session |
| PATCH | `/session/:id` | Update session |
| POST | `/session/:id/message` | Send message & wait |
| POST | `/session/:id/prompt_async` | Send message (async) |
| POST | `/session/:id/abort` | Abort running session |
| POST | `/session/:id/share` | Share session |
| GET | `/session/:id/diff` | Get session diff |

### Messages

| Method | Path | Description |
|--------|------|-------------|
| GET | `/session/:id/message` | List messages |
| POST | `/session/:id/message` | Send message |
| GET | `/session/:id/message/:messageID` | Get message details |
| POST | `/session/:id/command` | Execute slash command |
| POST | `/session/:id/shell` | Run shell command |

### Project & Files

| Method | Path | Description |
|--------|------|-------------|
| GET | `/project` | List all projects |
| GET | `/project/current` | Get current project |
| GET | `/path` | Get current path |
| GET | `/vcs` | Get VCS info |
| GET | `/file?path=<path>` | List files/directories |
| GET | `/file/content?path=<p>` | Read file |
| GET | `/find?pattern=<pat>` | Search in files |
| GET | `/find/file?query=<q>` | Find files by name |
| GET | `/find/symbol?query=<q>` | Find workspace symbols |
| GET | `/file/status` | Get tracked file status |

### Config & Providers

| Method | Path | Description |
|--------|------|-------------|
| GET | `/config` | Get config info |
| PATCH | `/config` | Update config |
| GET | `/config/providers` | List providers & models |
| GET | `/provider` | List all providers |
| GET | `/provider/auth` | Get auth methods |
| POST | `/provider/{id}/oauth/authorize` | OAuth authorize |

### Tools & Agents

| Method | Path | Description |
|--------|------|-------------|
| GET | `/agent` | List available agents |
| GET | `/experimental/tool/ids` | List tool IDs |
| GET | `/experimental/tool` | List tools with schemas |

### TUI Control

| Method | Path | Description |
|--------|------|-------------|
| POST | `/tui/append-prompt` | Append to prompt |
| POST | `/tui/submit-prompt` | Submit prompt |
| POST | `/tui/execute-command` | Execute command |
| GET | `/tui/control/next` | Wait for control request |
| POST | `/tui/control/response` | Respond to request |

### Other

| Method | Path | Description |
|--------|------|-------------|
| GET | `/command` | List all commands |
| GET | `/lsp` | LSP server status |
| GET | `/formatter` | Formatter status |
| GET | `/mcp` | MCP server status |
| POST | `/log` | Write log entry |
| GET | `/event` | SSE events stream |
| GET | `/doc` | OpenAPI spec |

---

## Provider Configuration

### Adding Providers

1. Configure provider in `opencode.json` or via `/connect` command
2. Add API keys using `/connect` - stored in `~/.local/share/opencode/auth.json`

### Config File Example

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "anthropic": {
      "options": {
        "baseURL": "https://api.anthropic.com/v1"
      }
    }
  }
}
```

### Environment Variables

```bash
# Anthropic
export ANTHROPIC_API_KEY=your_key

# OpenAI
export OPENAI_API_KEY=your_key

# Google
export GOOGLE_API_KEY=your_key
```

---

## Quick Start Example

```javascript
import { createOpencode } from "@opencode-ai/sdk"

async function main() {
  const { client } = await createOpencode()
  
  // Create a new session
  const session = await client.session.post({ 
    title: "My Task" 
  })
  
  // Send a message
  const response = await client.session({ id: session.id }).message.post({
    parts: [{ type: "text", text: "Hello, help me with my code" }]
  })
  
  console.log(response.parts)
}

main()
```

---

## See Also

- [OpenCode SDK Docs](https://opencode.ai/docs/sdk/)
- [OpenCode Server Docs](https://opencode.ai/docs/server/)
- [OpenCode Providers](https://opencode.ai/docs/providers/)
