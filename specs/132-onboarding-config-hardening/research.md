# Phase 0 Research: Onboarding config hardening

Feature: `132-onboarding-config-hardening` · Issue #180

This feature is a brownfield hardening pass over the example config, one wrapper script, and
one log line. There are no unknown technologies. The items below resolve the specific
behavioral questions the spec raised.

## R1 — Performer entry point: what is the correct portable default?

- **Decision**: Set the example default to `agent_executable: "bin/run-performer"`.
- **Rationale**: `bin/run-performer` is the canonical portable wrapper — it resolves the
  repo root relative to the script (`ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.."; pwd)"`),
  sources `.env`, and runs the performer via `uv run --project agent/performer`. Its own
  header already documents `agent_executable: "bin/run-performer"` as the intended usage.
- **Alternatives considered**:
  - `/usr/local/bin/performer` (current default) — does not exist on a fresh install. Rejected.
  - An absolute path built from the repo root — accurate but machine-specific; can't be a shipped default. Rejected for the default (mentioned in docs as an option).

## R2 — Relative-path resolution for `agent_executable`

- **Finding**: `SubprocessTransport._start()` calls
  `asyncio.create_subprocess_exec(self._executable, ...)` with **no `cwd`**
  (`src/coordinare/transport/subprocess_transport.py:201`). A path containing a slash (e.g.
  `bin/run-performer`) is therefore resolved relative to the **coordinare process's working
  directory**.
- **Implication**: `bin/run-performer` works when Coordinare is launched from the repo root.
  The canonical launch paths — `make run` / `make start` (and `bin/start`) — run from the
  repo root and source `.env`, so the relative default resolves correctly there.
- **Decision**: Ship the relative `bin/run-performer` default and document (quickstart /
  comment) that Coordinare is expected to be launched from the repo root, offering an
  absolute path as the alternative for non-standard launch locations. No code change to
  path resolution is in scope (that would be a behavior change beyond onboarding hardening).

## R3 — Notifications: why a fresh copy fails, and the minimal fix

- **Finding**: The example ships two *active* channels under `notifications.channels`
  (`config.example.yaml:742-760`): `slack-ops` (needs `${COORDINARE_SLACK_WEBHOOK_URL}`) and
  `email-team` (needs `${COORDINARE_SMTP_PASSWORD}`). `ChannelConfig._validate_type_fields`
  (`src/coordinare/config.py:110-118`) requires `webhook_url` for slack channels and
  `smtp_host` + `smtp_recipient` for email channels. A fresh operator with no secrets set
  trips validation on channels they never opted into.
- **Decision**: Change the **example file only** — ship `channels: []` and `routing: []`
  by default, and move the full Slack + email channel definitions into adjacent **commented**
  reference blocks. No change to `ChannelConfig` / `NotificationsConfig` validators.
- **Rationale**: The validators are correct — an active channel *should* require its
  credentials. The onboarding wall is that the example opts the user in by default. Fixing
  the example is the minimal, lowest-risk change and keeps the setup path discoverable
  (FR-005).

## R4 — Paused-symphony observability

- **Finding**: Disabled symphonies are already skipped, but the signal is a **per-cycle**
  warning: `daemon.py:2961-2963` runs inside the symphony loop and emits
  `logger.warning("symphony.disabled_skip", symphony=sym_name)` on **every** poll cycle.
  This is noisy and not the unambiguous startup message the issue asks for.
- **Decision**: Emit a clear, human-readable **startup** line per paused symphony (e.g.
  `symphony '<name>' is paused (enabled: false)`), once, and stop the per-cycle warning
  from recurring for the same symphony (track already-announced paused symphonies, or emit
  the summary where symphonies are first loaded at startup). Confirm the legacy
  single-symphony path (`daemon.py` else-branch, ~line 2972) is covered.
- **Rationale**: Satisfies FR-006 (unambiguous startup line) and FR-007 (no per-cycle
  spam). Behavior (pausing) is unchanged and already correct — this is observability only.

## R5 — Documentation surface (FR-008)

- **Finding**: `README.md` already lists `bin/run-performer` as the local performer runner
  (line 227) and does not hardcode the old `/usr/local/bin/performer` default. Historical
  `specs/**/quickstart.md` references to `agent_executable` are archival SDD records for
  prior features, **not** live onboarding docs, and are out of scope.
- **Decision**: Audit README / onboarding docs for any stale performer-path or
  notifications-default references and update only live onboarding docs to match the
  corrected example. Do not rewrite archived spec artifacts.

## Out of scope (explicit)

- Personal-account support / `github_org` user-vs-org handling and the
  `config.example.yaml:57` comment — that is **spec 133 / issue #181**, tracked separately.
- Any change to notification *validation* logic.
- Any change to `agent_executable` path-resolution semantics in the transport.
