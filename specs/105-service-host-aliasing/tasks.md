# Tasks: Resolve Declared Service Hostnames to Loopback In-Container

**Input**: design docs in `/specs/105-service-host-aliasing/`
**Tests**: INCLUDED — Constitution II (TDD).
**Organization**: by user story (US1 P1 single-label alias → US2 P2 URL-by-name → US3 P3 skip/idempotent). Single appended block in `render_activate_sh`. **No schema change, no new dep, no image rebuild.**

## Path Conventions

Coordinare: `src/coordinare/services/env_manifest.py` (`render_activate_sh`). Tests: `tests/unit/test_env_manifest.py`.

---

## Phase 1: Setup

- [X] T001 Re-read `render_activate_sh` (env_manifest.py) — the line list it builds, the spec-103 postgres-bin block at the end, and the existing `TestRenderActivateSh`-style tests in `tests/unit/test_env_manifest.py` (the `_manifest()` helper, `cache_mount_path` usage). Confirm where to append the new block.

---

## Phase 2: User Story 1 — single-label service hostname aliased to loopback (Priority: P1) 🎯 MVP

- [X] T002 [US1] Write FAILING tests in `tests/unit/test_env_manifest.py`: (a) content — the rendered `activate.sh` contains the alias loop (the `*_HOST`/`*_HOSTNAME` sed extraction, `127.0.0.1 $_hv`, the `COORDINARE_HOSTS_FILE` seam) and NO literal hostname value (secret-free); (b) behavioral — source the rendered script with `COORDINARE_HOSTS_FILE=<tmp>`, `POSTGRESQL_HOST=db`, `REDIS_HOST=redis`, `DEVENV=<nonexistent>` and assert the tmp hosts file gains `127.0.0.1 db` and `127.0.0.1 redis`. MUST fail before T003.
- [X] T003 [US1] In `render_activate_sh`, append the service-host aliasing block (per plan.md): `_HOSTS="${COORDINARE_HOSTS_FILE:-/etc/hosts}"`; for each single-label `*_HOST`/`*_HOSTNAME` env value, `grep -qw` idempotency then `echo "127.0.0.1 $_hv" >> "$_HOSTS" 2>/dev/null || true`; skip `""|localhost|*.*|*:*`. Make T002 pass.

**Checkpoint**: US1 — `db`/`redis` resolve to loopback; the app reaches coordinare-hosted services.

---

## Phase 3: User Story 2 — URL-embedded host resolves via the same name (Priority: P2)

- [X] T004 [US2] Write behavioral test: with `REDIS_HOST=redis` set (and `REDIS_SESSION_STORE_URL=redis://redis:46379/5/session` also present), the tmp hosts file gains `127.0.0.1 redis` so the URL host resolves. Confirms no URL parsing is needed (the name comes from `REDIS_HOST`). Should pass with T003.

---

## Phase 4: User Story 3 — skip real hosts + idempotent (Priority: P3)

- [X] T005 [US3] Write behavioral tests: (a) `SMTP_HOSTNAME=mail.example.com` (FQDN), `A_HOST=10.0.0.5` (IP), `B_HOST=localhost`, `C_HOST=` (empty) are NONE added to the tmp hosts file; (b) sourcing twice with `POSTGRESQL_HOST=db` yields exactly one `127.0.0.1 db` line (idempotent). Make pass.

---

## Phase 5: Polish & Cross-Cutting

- [X] T006 [P] Full `tests/unit/test_env_manifest.py` + coordinare suite green; confirm no regression to the spec-103 postgres-bin block or the rest of `render_activate_sh`.
- [X] T007 `.venv/bin/ruff check` edited files; walk `quickstart.md` A–E; confirm each SC has a covering test; verify secret-free (no baked hostname), best-effort/non-fatal, no new dep/schema/image change.
- [X] T008 A couple of adversarial review rounds (diverse-lens + refute-verify) before merge — focus: the sed extraction matches only `*_HOST`/`*_HOSTNAME` (not arbitrary vars), the skip cases are correct (FQDN/IP/localhost/empty), idempotency holds, best-effort never aborts activation, secret-free render, no regression to 103 block.

---

## Dependencies & Execution Order

- **US1 (P1)** = MVP (the alias block).
- **US2 (P2)** = consequence (URL resolves via the `*_HOST` name) — a test guard.
- **US3 (P3)** = skip/idempotent guards.
- **Polish** last.

## Implementation Strategy

MVP-first: **US1** — append the aliasing block to `render_activate_sh`. That alone makes `db`/`redis` resolve to the coordinare-hosted services. **US2/US3** are guards (URL-by-name; skip FQDN/IP/localhost/empty; idempotent; non-fatal). Host-side render → deploys via coordinare restart + re-bootstrap; no image rebuild. Closes the 102→103→104→105 chain so the website app connects to its DB.
