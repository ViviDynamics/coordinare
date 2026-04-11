# 041 — Operational Hardening — Implementation Plan

## Overview

Three targeted fixes for operational issues discovered during live e2e testing. All changes are backward compatible with no new dependencies.

## Architecture Decisions

### AD-1: STARTTLS for SMTP on port 587
`aiosmtplib.send()` supports `start_tls=True` which enables STARTTLS negotiation after connecting in plaintext. This is the correct mode for port 587 (submission). Port 465 uses implicit TLS (`use_tls=True`). We use `start_tls=True` since Mailtrap and most SMTP providers use port 587 with STARTTLS.

### AD-2: Phase-specific stuck thresholds with cooldown
The `per_phase_thresholds` config already exists but has no defaults for performer phases. We add sensible defaults (3600s for monitoring_performer/monitoring_agent) and track `last_stuck_alert_at` to implement repeat-alert throttling via a separate `cooldown_seconds` config (default 1800s) — so the alert fires once, then only repeats after the configured cooldown has elapsed.

### AD-3: Environment-based git auth
The performer workspace already implements this pattern. The coordinare workspace will adopt the same approach: `GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0=http.extraHeader`, `GIT_CONFIG_VALUE_0=Authorization: Basic {base64(x-access-token:{token})}`. The clone URL becomes a plain HTTPS URL with no credentials.

## Files Changed

| File | Changes |
|------|---------|
| `src/coordinare/services/notification.py` | Add `start_tls=True` and `timeout` to SMTP send |
| `src/coordinare/config.py` | Add default `per_phase_thresholds` for performer phases |
| `src/coordinare/daemon.py` | Add stuck alert cooldown tracking |
| `src/coordinare/workspace.py` | Replace URL-embedded token with env-based git auth |

## Risk Assessment

**Low risk** — all fixes are isolated, backward compatible, and address clear operational failures. SMTP fix can be verified against Mailtrap. Stuck alert changes only affect notification frequency. Git auth change matches an already-proven pattern from the performer.
