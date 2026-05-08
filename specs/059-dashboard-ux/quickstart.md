# Quickstart: Dashboard UX Redesign (059)

## Prerequisites

- Python 3.12+ with `uv` available
- `.env` file in project root with valid `GITHUB_*` and other coordinare config vars
- `config.yaml` configured (or `config.example.yaml` copied and edited)

## Running the Dashboard Locally

```bash
# Load environment
set -a && source .env && set +a

# Launch coordinare (dashboard auto-starts on port 8080)
uv run python -m coordinare
```

Dashboard is available at: `http://localhost:8080`

## Verifying Each UX Change

### 1. Status at a Glance (P1)

Open `http://localhost:8080` with no active cards:
- Expect a single **Idle** panel — not a collection of empty cards
- Expect last poll time and cycle count displayed

With an active card in flight:
- Active card panel shows **human-readable** phase label (e.g. "Monitoring Agent", not "monitoring_agent")
- Elapsed time displayed alongside the card
- Cost estimate shown on the card row (or "—" if session just started)

With a PR awaiting review:
- Awaiting Review panel is visually distinct (distinct border/header) from Active Work
- A clear "Needs your attention" indicator is present

### 2. Navigation (P2)

At 1280px viewport width:
- All nav links visible without hamburger click
- Click each nav link — verify the active link gets the blue underline indicator
- Hard-reload `/history` directly — verify History nav item is highlighted on load

### 3. Card Detail (P3)

Click an active card row:
- Detail view shows: phase, assigned performer, elapsed time, cost, last log lines
- If elapsed > 30 min: elapsed value shows ⚠ prefix and warning colour

### 4. Small Screen (P4)

Resize browser to 768px:
- No horizontal scrollbar on any page
- All primary panels readable
- Navigate to Symphonies and Config pages — tables wrap or scroll within their containers

### 5. Health Summary

All subsystems healthy:
- Single green "● All systems healthy" line — no expanded table by default

Simulate a degraded subsystem (edit `health.py` or use a mock):
- Summary line shows ⚠ count and is expanded by default
- Detail row shows the plain-language `details` message

### 6. SSE Disconnect

Terminate the coordinare process while the browser is open:
- Within 5 seconds, the nav status dot turns red
- The disconnected banner (if present) or dot update is visible without page reload

Restart coordinare:
- Status dot returns to green automatically on reconnection

## Running Tests

```bash
# Unit tests only (fast)
uv run --extra dev pytest tests/unit/ -v

# Full suite with coverage
uv run --extra dev pytest --cov=coordinare --cov-report=term-missing --cov-fail-under=90

# Lint
uv run --extra dev ruff check src tests
```

## Key Files

| File | Purpose |
|---|---|
| `src/coordinare/dashboard.py` | All dashboard HTML/CSS/JS + Python helpers |
| `tests/unit/test_dashboard.py` | Unit tests for Python dashboard helpers |

## Implementation Notes for Contributors

- **Never** use raw hex colour values in CSS or inline `style=` attributes — always use `var(--color-*)` tokens defined in `:root`
- **Always** pass raw `phase` strings through `formatPhaseLabel()` (JS) or `format_phase_label()` (Python) before displaying to the operator
- When adding new JS-generated HTML, use `esc()` for all user-supplied or server-supplied string values
- The `isIdle()` check (`active_sessions.length === 0 && phase === "idle"`) gates the idle state panel — if this returns true, no per-session panels should be shown
- Stale detection: `elapsedMs > 30 * 60 * 1000` — use the constant, don't inline the literal
