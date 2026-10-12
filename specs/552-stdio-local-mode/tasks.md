# Tasks — 552 stdio local mode

- [ ] 1. Config widening (`test_552_mcp_stdio_config.py`): `McpServerConfig(transport="stdio")` validates; `from_env` round-trips `COORDINARE_MCP_TRANSPORT=stdio`.
- [ ] 2. CLI dispatch (`test_552_mcp_stdio_main.py`): `--transport stdio` invokes the stdio runner and returns 0; `--transport stdio --listen-port 8091` exits 2; http mode unchanged.
- [ ] 3. stdio session (`test_552_mcp_stdio_server.py`): `run_stdio(config)` serves the full 16-tool registry with no bearer gate; caller fingerprint is the agent-token fingerprint on audit lines.
- [ ] 4. Docs: `docs/mcp.md` transport section; spec quickstart note.
- [ ] 5. Full unit suite + ruff + baseline + mypy; preflight; PR.
