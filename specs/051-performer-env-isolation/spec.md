# Feature Specification: Performer Environment Isolation

**Feature Branch**: `051-performer-env-isolation`
**Created**: 2026-04-21
**Status**: Draft
**Input**: The performer subprocess currently inherits the host's full environment variables. During live testing, the performer was using the host user's personal GITHUB_TOKEN and git identity (name/email) instead of the GitHub App token and the bot identity. This caused commits attributed to the wrong user, token scope mismatches, and potential secret leakage into subprocess logs.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Performer uses App token and bot identity (Priority: P1)

When coordinare launches a performer subprocess (e.g. opencode, claude_code), the subprocess environment is explicitly constructed — it does NOT inherit the host env. The environment contains only what the performer needs: the GitHub App installation token, the correct `GIT_AUTHOR_NAME`/`GIT_AUTHOR_EMAIL`/`GIT_COMMITTER_*` for the bot identity, `HOME` (for tool config), `PATH` (for binaries), and any explicitly allow-listed vars from config.

**Why this priority**: During live testing, commits from performers appeared under the host developer's name and email. The performer also picked up the host user's `GITHUB_TOKEN` env var, which has different scopes than the App token, causing silent auth failures on some API calls.

**Independent Test**: Run coordinare with a GitHub App token configured. Dispatch a card. Inspect the git log on the branch created by the performer. Verify commits are attributed to the configured bot identity (not the host user), and the author email matches the App bot's email.

**Acceptance Scenarios**:

1. **Given** coordinare is configured with a GitHub App and `bot_name: coordinare-bot` / `bot_email: coordinare-bot@example.com`, **When** a performer commits code, **Then** the commit author and committer are `coordinare-bot <coordinare-bot@example.com>`.
2. **Given** the host environment has a personal `GITHUB_TOKEN` set, **When** the performer subprocess runs in App mode, **Then** the subprocess does NOT inherit the host `GITHUB_TOKEN` — no `GITHUB_TOKEN` is set in the subprocess env at spawn time; the App installation token is delivered via the `dispatch_card` protocol message instead.
3. **Given** `env_passthrough: [ANTHROPIC_API_KEY, OPENCODE_MODEL]` in config, **When** the performer subprocess runs, **Then** those specific vars are passed through in addition to the required set.
4. **Given** any other env var is set on the host (e.g. `AWS_SECRET_ACCESS_KEY`), **When** the performer subprocess runs, **Then** that var is NOT present in the subprocess environment.

---

### User Story 2 — Configurable bot identity (Priority: P2)

The bot git identity (name, email) is configurable in `config.yaml` under a `bot_identity` section. Sensible defaults are provided so existing setups continue to work without config changes.

**Independent Test**: Set `bot_identity.name: my-bot` and `bot_identity.email: bot@company.com`. Dispatch a card and check git log. Verify identity matches config.

**Acceptance Scenarios**:

1. **Given** `bot_identity.name` and `bot_identity.email` are set in config, **When** performer commits, **Then** git identity matches config values.
2. **Given** no `bot_identity` in config, **When** performer commits, **Then** a sensible default is used (e.g. `"Coordinare Bot" <coordinare@localhost>`).

---

## Functional Requirements

- **FR-001**: In the transport layer (or workspace setup), construct the subprocess env explicitly rather than using `os.environ` or inheriting the full env. Start from a minimal base: `PATH`, `HOME`, `TMPDIR`/`TEMP`, `LANG`/`LC_ALL`.
- **FR-002**: Always inject `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME`, `GIT_COMMITTER_EMAIL` from the configured `bot_identity` (or defaults).
- **FR-003**: Inject the GitHub token as the var the performer expects (e.g. `GITHUB_TOKEN`) using the App installation token, not the host env.
- **FR-004**: Add `env_passthrough: list[str]` to `CoordinareConfig` (default: empty list). Named vars are copied from host env to subprocess env if present on the host.
- **FR-005**: Add `bot_identity.name` and `bot_identity.email` to `CoordinareConfig` with defaults `"Coordinare Bot"` and `"coordinare@localhost"`.
- **FR-006**: Log a structured `subprocess_transport.env_constructed` event at DEBUG level listing the env var names (not values) provided to the subprocess.

## Non-Goals

- Container/namespace isolation (out of scope — this is env-var scope only).
- Per-role env overrides.
- Secrets management integration (Vault, AWS SM, etc.).
