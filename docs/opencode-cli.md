# OpenCode CLI Documentation

## Installation

```bash
curl -fsSL https://opencode.ai/install | bash
```

Or via npm:
```bash
npm install -g opencode-ai
```

## Core Commands

| Command | Description |
|---------|-------------|
| `opencode` | Start TUI |
| `opencode [project]` | Start TUI in project directory |
| `opencode run "prompt"` | Run in non-interactive mode |
| `opencode serve` | Start headless HTTP server |
| `opencode web` | Start web interface |
| `opencode auth login` | Configure API keys |
| `opencode session list` | List sessions |
| `opencode export [id]` | Export session as JSON |
| `opencode upgrade` | Update to latest version |

## Global Flags

- `--help`, `-h` - Display help
- `--version`, `-v` - Print version
- `--print-logs` - Print logs to stderr
- `--log-level` - DEBUG, INFO, WARN, ERROR

## Common Flags for `opencode run`

| Flag | Short | Description |
|------|-------|-------------|
| `--continue` | `-c` | Continue last session |
| `--session` | `-s` | Continue specific session |
| `--model` | `-m` | Model to use |
| `--attach` | - | Attach to running server |
| `--file` | `-f` | Files to attach |
| `--fork` | - | Fork session when continuing |
| `--share` | - | Share session |
| `--title` | - | Title for session |
| `--port` | - | Port for local server |

## Timeout Configuration

### Environment Variable (Experimental)

Set default timeout for all bash commands:

```bash
export OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS=3600000
opencode run "your command"
```

This sets a 1-hour timeout (3,600,000ms).

### Tool Parameter

When using OpenCode via API/LLM tool calls, pass timeout as a parameter:

```json
{
  "command": "your command",
  "timeout": 3600000
}
```

Default timeout: **120,000ms (2 minutes)**

### Timeout Examples

| Duration | Milliseconds |
|----------|--------------|
| 2 minutes | 120000 |
| 5 minutes | 300000 |
| 10 minutes | 600000 |
| 30 minutes | 1800000 |
| 1 hour | 3600000 |

## Environment Variables

| Variable | Type | Description |
|----------|------|-------------|
| `OPENCODE_CONFIG` | string | Config file path |
| `OPENCODE_SERVER_PASSWORD` | string | Basic auth for serve/web |
| `OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS` | number | Default command timeout |
| `OPENCODE_AUTO_SHARE` | boolean | Automatically share sessions |
| `OPENCODE_DISABLE_AUTOUPDATE` | boolean | Disable auto update |
| `OPENCODE_ENABLE_EXA` | boolean | Enable Exa web search |
| `OPENCODE_MODELS_URL` | string | Custom models URL |

## MCP Servers

Add and manage Model Context Protocol servers:

```bash
opencode mcp add          # Add MCP server
opencode mcp list        # List configured servers
opencode mcp auth [name] # Authenticate with OAuth server
```

## Sessions

```bash
opencode session list           # List all sessions
opencode session list -n 10    # Limit to 10 most recent
opencode export [sessionID]   # Export session as JSON
```

## Web Server

Start headless server with web interface:

```bash
opencode web --port 4096 --hostname 0.0.0.0
```

Set basic authentication:
```bash
export OPENCODE_SERVER_PASSWORD=your-password
```

## Attaching to Remote Server

Attach TUI to a running backend server:

```bash
opencode attach http://10.20.30.40:4096
```

## Agent Management

```bash
opencode agent create    # Create new agent
opencode agent list     # List available agents
```

## Authentication

```bash
opencode auth login     # Configure API keys
opencode auth list      # List authenticated providers
opencode auth logout   # Logout from provider
```

## Models

```bash
opencode models                    # List all available models
opencode models anthropic          # Filter by provider
opencode models --refresh           # Refresh models cache
opencode models --verbose          # Verbose output with costs
```

## ACP (Agent Client Protocol)

Start ACP server for stdin/stdout communication:

```bash
opencode acp --port 4096 --hostname 0.0.0.0
```

## GitHub Integration

```bash
opencode github install    # Install GitHub agent
opencode github run      # Run GitHub agent
```

## Upgrade

```bash
opencode upgrade            # Upgrade to latest
opencode upgrade v0.1.48   # Upgrade to specific version
```

## Uninstall

```bash
opencode uninstall           # Uninstall
opencode uninstall --keep-config  # Keep config files
opencode uninstall --dry-run      # Show what would be removed
```