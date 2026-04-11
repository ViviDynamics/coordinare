# 041 — Operational Hardening

## Summary

Three operational issues discovered during live e2e testing (spec 040) that were deferred as out-of-scope. This spec addresses email delivery failures, overly aggressive stuck-card alerts, and token exposure in git process arguments.

## Motivation

During live testing against ViviDynamics/website, email notifications consistently failed with "Unexpected EOF received", stuck-card alerts fired every 30 seconds during normal codex sessions (30+ minutes), and Copilot flagged the coordinare's clone URL as a security concern (token in process argv).

## Issues

### 1. Email SMTP Delivery Failure

**Symptom**: Every email notification fails with `Unexpected EOF received` when sending to Mailtrap (sandbox.smtp.mailtrap.io:587).

**Root cause**: `aiosmtplib.send()` is called without TLS parameters. Port 587 requires STARTTLS, but the library attempts a plaintext handshake. The server closes the connection expecting TLS negotiation.

**Fix**: Add `start_tls=True` (STARTTLS for port 587) and a `timeout` parameter to the `aiosmtplib.send()` call in `EmailChannelSender.send()`.

**File**: `src/coordinare/services/notification.py` (lines 90-102)

### 2. Card Stuck Alert Threshold

**Symptom**: `card_stuck` notifications fire every 30-second poll cycle once the threshold is exceeded, producing dozens of identical alerts during normal long-running codex sessions.

**Root cause**: The default `threshold_seconds` is 1800 (30 min) and `per_phase_thresholds` is empty. Codex implementing sessions routinely run 30-40 minutes, triggering the alert. Additionally, the alert fires on every poll cycle with no cooldown.

**Fix**:
- Set higher default thresholds for `monitoring_performer` and `monitoring_agent` phases (e.g. 3600s / 60 min).
- Add a cooldown so the stuck alert fires at most once per threshold window, not every poll cycle.

**Files**: `src/coordinare/config.py`, `src/coordinare/daemon.py`

### 3. Token Exposure in Coordinare Workspace Clone URL

**Symptom**: The GitHub token is embedded in the git clone URL (`https://x-access-token:{token}@github.com/...`), exposing it in `/proc/*/cmdline` and process monitors.

**Root cause**: The coordinare workspace uses URL-embedded auth while the performer workspace already uses the safer `GIT_CONFIG_VALUE_0=Authorization: Basic {encoded}` environment approach.

**Fix**: Replace the URL-embedded token with environment-based `http.extraHeader` auth, matching the performer workspace pattern. Use plain `https://github.com/{org}/{project}.git` as the clone URL.

**File**: `src/coordinare/workspace.py`

## Out of Scope
- Dashboard backend transparency (surfacing codex web UI URLs)
- Notification message template customization beyond the existing config
