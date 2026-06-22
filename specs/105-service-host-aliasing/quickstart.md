# Quickstart: Service-Host Aliasing

Replays the website connection gap and the guards as acceptance scenarios.

## Scenario A — US1: compose hostname resolves to loopback (SC-001)
1. Activation runs with `POSTGRESQL_HOST=db`, `REDIS_HOST=redis` and coordinare-hosted postgres/redis on 127.0.0.1.
2. **Verify:** `/etc/hosts` gains `127.0.0.1 db` and `127.0.0.1 redis`; the app (which reads `POSTGRESQL_HOST=db`) now connects to the coordinare-hosted DB.

## Scenario B — US2: URL-embedded host resolves too (SC-001)
1. `REDIS_SESSION_STORE_URL=redis://redis:46379/5/session` with `REDIS_HOST=redis`.
2. **Verify:** the `redis` alias makes the URL resolve to loopback — no separate URL parsing.

## Scenario C — US3: skip real hosts (SC-002)
1. `SMTP_HOSTNAME=mail.example.com` (FQDN), `X_HOST=10.0.0.5` (IP), `Y_HOST=localhost`, `Z_HOST=` (empty).
2. **Verify:** none are added to `/etc/hosts`.

## Scenario D — US3: idempotent (SC-003)
1. Activate twice with `POSTGRESQL_HOST=db`.
2. **Verify:** `/etc/hosts` has exactly one `127.0.0.1 db` line.

## Scenario E — secret-free + non-fatal (SC-004)
1. Inspect the rendered `activate.sh`.
2. **Verify:** it contains the generic `*_HOST`/`*_HOSTNAME` alias loop but no literal hostname value; a read-only hosts file does not abort activation.

## Real-world payoff
The website app reads `POSTGRESQL_HOST=db` / `REDIS_HOST=redis`; after activation those resolve to the coordinare-hosted postgres/redis on 127.0.0.1, so DB-backed feature/QA tests connect instead of failing "connection refused" — the last link in the 102→103→104→105 chain.
