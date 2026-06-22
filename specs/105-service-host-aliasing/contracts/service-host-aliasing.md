# Contract: Service-Host Aliasing in activate.sh

Governs the runtime block `render_activate_sh` emits to alias declared service hostnames to loopback.

## Field Registry

(No dispatch/payload field changes — rendered-script behavior only. Registry intentionally empty.)

## Rendered-block contract

| Aspect | Requirement |
|---|---|
| Source of names | values of env vars whose name ends in `_HOST` or `_HOSTNAME`, read from the LIVE env at runtime |
| Aliased target | `127.0.0.1 <name>` appended to `${COORDINARE_HOSTS_FILE:-/etc/hosts}` |
| Skipped values | empty, `localhost`, FQDN (`*.*`), IP / `host:port` / IPv6 (`*:*`) |
| Idempotency | skip if the name is already a whole-word entry in the hosts file (`grep -qw`) |
| Safety | best-effort: a non-writable hosts file (or any failure) MUST NOT abort activation (`|| true`, `2>/dev/null`) |
| Secret-free | the rendered script is a generic loop; NO test-env value is baked in |

## Invariants (MUST)

1. **Single-label service hostnames resolve to loopback (FR-001/FR-002, SC-001):** `POSTGRESQL_HOST=db` ⇒ `/etc/hosts` gains `127.0.0.1 db`; URL-embedded `redis://redis:.../` resolves via the `redis` alias from `REDIS_HOST` (no URL parsing).
2. **No clobbering (FR-003, SC-002):** FQDN/IP/`localhost`/empty values are never aliased.
3. **Idempotent (FR-004, SC-003):** re-activation adds no duplicate entry.
4. **Non-fatal (FR-005, SC-004):** a non-writable hosts file does not abort activation.
5. **Secret-free / no new dep / image service-agnostic (FR-006/FR-007):** rendered script carries the loop, not values; reuses /etc/hosts + activate.sh; binaries/services ride the cache.
