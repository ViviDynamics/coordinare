# Quickstart: Workflow State Persistence

**Feature**: 003-state-persistence
**Branch**: `003-state-persistence`

This guide describes how to work with the state persistence feature: configuration, normal operation, crash recovery verification, and troubleshooting.

---

## Configuration

State persistence is controlled by a single config field with a sensible default.

**`config.yaml`** (or environment variable):

```yaml
# Default: ./coordinare.state.json (relative to coordinare's WORKDIR)
state_file_path: /var/coordinare/coordinare.state.json
```

**Environment variable override**:

```bash
export COORDINARE_STATE_FILE_PATH=/var/coordinare/coordinare.state.json
```

The path must be on a filesystem that persists across container restarts (bind mount or named volume). The coordinare's WORKDIR is mounted independently from any agent subprocess WORKDIRs.

---

## First Run

On first start, no state file exists. The coordinare logs an info message and begins a fresh idle cycle:

```
INFO  state_store  event="no prior state found" path="./coordinare.state.json"
INFO  runtime_event  category="startup"  message="daemon startup complete"
```

---

## Normal Operation

After every phase transition, the coordinare writes a snapshot. You can observe the write in structured logs:

```
INFO  state_store  event="state written"  phase="monitoring_agent"  path="./coordinare.state.json"  duration_ms=3.2
```

To inspect the current persisted state at any time:

```bash
cat ./coordinare.state.json | python3 -m json.tool
```

Example output:

```json
{
  "schema_version": 1,
  "snapshot_at": "2026-02-22T14:30:00.000000Z",
  "phase": "monitoring_agent",
  "active_card_id": "PVT_kwDOABCDEF",
  "active_card_title": "Implement user login flow",
  "active_card_column": "In Progress",
  "pr_url": null,
  "pr_node_id": null,
  "agent_session_id": "sess_abc123",
  "open_questions": []
}
```

The health endpoint also exposes the current phase and last write timestamp immediately:

```bash
curl -s http://localhost:8080/health | python3 -m json.tool
```

```json
{
  "status": "healthy",
  "phase": "monitoring_agent",
  "snapshot_at": "2026-02-22T14:30:00.000000Z",
  ...
}
```

---

## Crash Recovery Verification

To manually verify crash recovery:

1. **Start the coordinare** with a card In Progress on the board:
   ```bash
   uv run coordinare
   ```

2. **Wait** for the coordinare to pick up the card and transition to `monitoring_agent`. Verify the state file is written:
   ```bash
   cat ./coordinare.state.json
   ```

3. **Kill the process uncleanly**:
   ```bash
   pkill -9 coordinare  # or kill -9 <pid>
   ```

4. **Restart**:
   ```bash
   uv run coordinare
   ```

5. **Verify recovery** in logs:
   ```
   INFO  state_store   event="prior state loaded"  phase="monitoring_agent"  card_id="PVT_..."
   INFO  state_store   event="board reconciliation passed"  phase="monitoring_agent"
   INFO  runtime_event  category="startup"  message="daemon startup complete"
   ```

6. **Verify no re-dispatch** — the coordinare resumes monitoring without dispatching the card again. The card remains In Progress on the board (not re-moved to a previous column).

---

## Corrupted State File

To test corrupted-state handling:

```bash
echo "not valid json" > ./coordinare.state.json
uv run coordinare
```

Expected output (warning, not crash):

```
WARN  state_store  event="state load failed"  reason="corrupt"  path="./coordinare.state.json"
INFO  runtime_event  category="startup"  message="daemon startup complete"
```

The coordinare discards the invalid file and starts fresh.

---

## Permission Error

If the state file path is not writable, the coordinare exits immediately at startup:

```
ERROR  state_store  event="state path not writable"  path="/read-only/coordinare.state.json"
```

Exit code: `1`

Fix by ensuring the path is on a writable volume or correcting the `state_file_path` setting.

---

## Disk-Full Scenario

If a state write fails due to a full disk, the coordinare logs a warning and continues running with persistence degraded:

```
WARN  state_store  event="state write failed"  reason="[Errno 28] No space left on device"
```

The previous state file (if any) remains intact on disk. The coordinare continues operating in memory. Resolve by freeing disk space — the next successful phase transition will write a new state file.

---

## Metrics

State persistence exposes three Prometheus metrics at `GET /metrics`:

| Metric | Type | Description |
|---|---|---|
| `coordinare_state_write_duration_seconds` | Histogram | Latency of each atomic write |
| `coordinare_state_write_failures_total` | Counter | Count of failed write attempts |
| `coordinare_state_last_written_timestamp` | Gauge | Unix timestamp of last successful write |

**Staleness alert example** (PromQL):

```promql
time() - coordinare_state_last_written_timestamp > 300
```

Fires if the coordinare has not written state for 5 minutes (e.g., stuck in a long idle cycle or persistence silently degraded).

---

## Development: Running Tests

```bash
# Unit tests for StateStore
cd src && pytest tests/unit/test_state_store.py -v

# Integration: crash recovery
cd src && pytest tests/integration/test_crash_recovery.py -v

# Contract: health response schema
cd src && pytest tests/contract/test_health_schema.py -v
```
