# Quickstart / Verification: Onboarding config hardening

Feature `132-onboarding-config-hardening` · Issue #180. These are the manual + automated
checks that prove each acceptance criterion. Run after `/speckit.implement`.

## Prerequisites

- Clean checkout of the branch; `.venv` set up (`make install` / `bin/install`).
- **No** `COORDINARE_SLACK_WEBHOOK_URL` / `COORDINARE_SMTP_PASSWORD` in the environment (simulates a fresh operator).

## US1 — Performer launches from an unedited example (P1)

1. `cp config.example.yaml /tmp/fresh-config.yaml` and set only the required identity fields (github token/org, a project number).
2. Confirm the shipped default is `agent_executable: "bin/run-performer"`.
3. From the **repo root**, start Coordinare against the fresh config and confirm the performer process launches (no `No module named performer`, no missing-binary error), with **zero** edits to any `bin/` script.
4. `grep -rn "/Users/" bin/` returns nothing — no developer-specific absolute path is checked in.
5. Confirm `bin/performer` still exists but is now a minimal wrapper that delegates to `bin/run-performer` (no duplicated launch logic, no home path); invoking `bin/performer` behaves like `bin/run-performer`.

## US2 — Fresh config validates without secrets (P2)

1. With no Slack/SMTP secrets in the env, load `/tmp/fresh-config.yaml` through config validation (start the daemon or the config-load path).
2. Validation succeeds — no "webhook_url required" / "smtp_host and smtp_recipient required" error.
3. Confirm `notifications.channels` and `notifications.routing` are `[]` in the shipped example, and that commented Slack + email reference blocks remain in the file.

## US3 — Paused symphony is obvious at startup (P3)

1. Configure one symphony with `enabled: false`.
2. Start Coordinare and inspect the startup logs.
3. Exactly one clear line appears, e.g. `symphony '<name>' is paused (enabled: false)`.
4. Let several poll cycles elapse — the pause line does **not** repeat every cycle.
5. Repeat with the legacy single-symphony config shape to confirm coverage.

## Automated checks

```sh
make test-all        # includes tests/unit/test_132_onboarding_config_hardening.py
make lint            # ruff clean
```

The new test module asserts: the shipped `config.example.yaml` validates with an empty
notification-secret environment; the example `agent_executable` resolves to an existing,
executable `bin/run-performer`; and a `enabled: false` symphony yields one startup pause
line with no per-cycle recurrence.

## Done when

- SC-001…SC-005 in [spec.md](./spec.md) all hold.
- `make test-all` and `make lint` are green.
- No checked-in file contains a developer-specific absolute performer path.
