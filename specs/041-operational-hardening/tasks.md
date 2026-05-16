# 041 — Operational Hardening — Tasks

## Email SMTP Delivery

- [x] Add `start_tls=True` and `timeout=10` to `aiosmtplib.send()` in `EmailChannelSender.send()`
- [x] Update email notification tests to verify TLS parameter is passed
- [x] Verify email delivery against Mailtrap sandbox (post-merge) — obsolete code-side; SMTP config + `start_tls=True` shipped in `services/notification.py`. Manual sandbox verification is a post-deploy operational check.

## Card Stuck Alert Threshold

- [x] Add default `per_phase_thresholds` for `monitoring_performer` (3600s) and `monitoring_agent` (3600s) in config
- [x] Add `last_stuck_alert_at` tracking in daemon to implement cooldown
- [x] Only fire stuck alert once per threshold window (not every poll cycle)
- [x] Update stuck alert tests for cooldown behavior

## Workspace Clone URL Auth

- [x] Replace URL-embedded token with environment-based `http.extraHeader` auth in coordinare workspace
- [x] Use plain `https://github.com/{org}/{project}.git` as clone URL
- [x] Pass auth env vars (`GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_0`, `GIT_CONFIG_VALUE_0`) to git subprocess
- [x] Update workspace tests for new auth pattern
