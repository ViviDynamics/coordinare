# 044 — Transport Resilience & Relay Recovery — Research

## R-1: How does the transport currently parse responses?

**Finding:** `subprocess_transport.py:194-204` reads a single line via `proc.stdout.readline()`, strips whitespace, and calls `ProtocolResponse.model_validate_json(data.strip())`. Any non-JSON content (ANSI codes, bare strings, log lines) causes an immediate `TransportError`.

**Decision:** Skip non-JSON lines, accept first valid JSON. The protocol is line-delimited — each valid response is one complete JSON object on its own line. Non-JSON lines are noise from subprocesses that leaked to stdout.

**Rationale:** Simpler than JSON extraction (which could match false-positive JSON-like log output). Matches the protocol's design intent. Valid responses parse identically to before.

## R-2: Where does the stdout contamination come from?

**Finding:** The performer correctly configures structlog to stderr (`main.py:1102-1103`). But the codex backend launches subprocesses via its tool-use mechanism. Those subprocesses (rubocop, bundler) inherit the performer's stdout — which IS the protocol channel. The `_run_ci_check` helper uses `asyncio.subprocess.PIPE` (captures correctly), but the backend's independent tool-use doesn't.

**Decision:** Two-pronged fix: (1) make transport resilient to noise (AD-1), (2) tell personas "do NOT run CI commands yourself" so the backend doesn't independently run rubocop via tool-use.

## R-3: Does a relay retry budget already exist?

**Finding:** `system_error_count` in state increments on TransportError/ConnectionError/TimeoutError (monitor_performer.py:509). It resets to 0 when entering monitoring_pr (line 334). But there's no threshold check that routes to blocked — the count just accumulates and resets.

**Decision:** Add a check in the session_expired handler: if `system_error_count >= 3` AND the card is in a relay flow (has relay_feedback or previous_status == "IN_REVIEW"), transition to blocked instead of re-dispatching. This reuses existing state without new fields.

## R-4: How does the tech writer currently commit?

**Finding:** `performer/main.py:917-929` loops over `doc_files` and calls `commit_file()` per file. Each `commit_file` does git add + commit + push — one push per file. This produced 14 commits with the same "docs: update documentation" message on PR #94.

**Decision:** Add `commit_files(stand, files, message)` that stages all files, commits once, pushes once. Tech writer handler calls this instead of the loop. Existing `commit_file` stays for single-file roles (assessor, architect, etc.).

## R-5: How to verify CI tools are installed?

**Finding:** `ci_detection.py` returns commands like `bundle exec rubocop` based on file presence, but doesn't verify the tool is actually installed. In a fresh clone without `bundle install`, running rubocop produces Bundler errors that leak to stdout.

**Decision:** After detection, run `subprocess.run(shlex.split(cmd + " --version"), capture_output=True, timeout=5)`. If exit code != 0 or timeout, set `lint_command=None`. This is a one-time 5s check per detection call (once per lifecycle stage). Tools that support `--version`: rubocop, ruff, flake8, eslint, pytest, npm. Make targets don't support `--version` — skip verification for make.
