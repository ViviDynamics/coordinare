# Research: Resolving Declared Service Hostnames to Loopback

## Decision: alias single-label `*_HOST`/`*_HOSTNAME` values to 127.0.0.1 in /etc/hosts at activation

The coordinare-owned `activate.sh` (sourced in-container with the live test-env) appends `127.0.0.1 <name>` to `/etc/hosts` for each single-label value of a `*_HOST`/`*_HOSTNAME` env var. Best-effort, idempotent, secret-free (reads the live env; bakes no value into the script).

**Rationale**:
- Fundamentally a name-resolution problem: the app resolves `db`/`redis` by name (in standalone vars AND embedded in URLs like `redis://redis:.../`). Fixing the NAME fixes every use site.
- No URL parsing needed: every hostname the app must reach is also declared by a `*_HOST` var (website: `POSTGRESQL_HOST=db`, `REDIS_HOST=redis`). Aliasing the name covers the URL too.
- Host-side render (env_manifest.py) → ships via coordinare restart + re-bootstrap; no image rebuild.
- Reads the live env at runtime → the rendered script is a generic loop with no secret value (secret invariant).

## Alternatives considered

- **Override `POSTGRESQL_HOST=127.0.0.1` in the injected env**: rejected — misses the hostname embedded in `REDIS_SESSION_STORE_URL=redis://redis:.../` (the var override doesn't touch URL strings). Name resolution covers both.
- **Catch-all single-label resolver (dnsmasq / NSS / `address=/#/`)**: rejected — masks typos (a misspelled host silently resolves to loopback), and a Dockerfile-level resolver applies image-wide to every project unconditionally.
- **Sidecar containers (docker-in-docker)**: rejected — needs DinD/host-socket (privilege/security), is heavyweight, and undoes the 091/102 cache-based design (binaries ride the cache, image service-agnostic).
- **Bake the alias list into the rendered activate.sh host-side**: rejected — would embed test-env values (hostnames) into the script, and the test-env is secret-channel material; the runtime loop reads the live env instead.
- **Write /etc/hosts from a Python coordinare node**: rejected — the write must happen in-container at runtime where the live test-env exists; activate.sh already runs there.

## Confirmed facts (2026-06-22)

- Performer container runs as **root**; `/etc/hosts` is writable (`docker run … touch /etc/hosts` → WRITABLE). A simple append works; the block is still best-effort if that changes.
- `test_env_loader` loads `.env.test` **verbatim** — `POSTGRESQL_HOST=db`, `REDIS_HOST=redis`, `REDIS_SESSION_STORE_URL=redis://redis:46379/...`; no host remap anywhere in coordinare/performer.
- Coordinare runs the website's tests **in-container on localhost** (the only docker-compose usage, `screenshot_service.py`, is dead/unwired).
- `config/database.yml` defaults `POSTGRESQL_HOST` to `localhost` only when **unset**; it is set to `db`, so the default never applies.
