# Quickstart: Performer Environment Caching (060)

## Prerequisites

- Coordinare running with at least one symphony configured (spec 057).
- At least one containerized performer registered in `orchestra.performers` (spec 056).
- A Docker image that acts as your `env_bootstrap` performer (operator-provided; must accept `POST /jobs` with `job_type: env_bootstrap` and install tooling into `/devenv`).

---

## 1. Add config

In `config.yaml`, add the global cache root and wire an env_bootstrap performer:

```yaml
# Global (default) config — add env_cache_root
env_cache_root: ~/.coordinare/env-caches/   # optional, this is the default

# Register your env_bootstrap performer in the orchestra
orchestra:
  performers:
    - id: bootstrap-py
      mode: ephemeral
      image: myorg/env-bootstrap:latest
      roles:
        - env_bootstrap
      readiness_timeout_s: 300   # bootstrap runs can take a few minutes

# Per-symphony: reference the env_bootstrap performer
symphonies:
  - name: my-project
    github_org: myorg
    github_repo: my-project
    github_project_number: 1
    env_bootstrap_performer_id: bootstrap-py
    env_spec_files: [README.md]    # optional, this is the default
```

---

## 2. Start coordinare

```bash
set -a && source .env && set +a
python -m coordinare
```

On the first poll cycle, coordinare:
1. Fetches the blob SHA of `README.md` for `my-project`.
2. Detects no prior SHA → triggers an `env_bootstrap` dispatch to `bootstrap-py`.
3. Creates `~/.coordinare/env-caches/my-project/` before starting the container.
4. Mounts it at `/devenv` (read-write) inside the bootstrap container.

---

## 3. Verify bootstrap ran

```bash
ls ~/.coordinare/env-caches/my-project/
# Should show tools/runtimes installed by your bootstrap image
```

Check coordinare logs:
```
env_cache.sha_changed  symphony=my-project old_sha=null new_sha=abc1234
env_cache.bootstrap_dispatched  symphony=my-project performer_id=bootstrap-py
env_cache.bootstrap_complete  symphony=my-project success=True
```

---

## 4. Dispatch a regular performer

On the next card dispatch for `my-project`, coordinare automatically mounts the env cache and injects the path into the job payload:

```
performer.start  performer_id=some-performer volumes=["/devenv/my-project-a1b2c3:ro"]
env_cache.volume_injected  symphony=my-project container_path=/devenv/my-project-a1b2c3
```

The job init payload sent to the performer includes:

```json
{ "metadata": { "env_cache_path": "/devenv/my-project-a1b2c3" } }
```

The performer image sources the activation script at that path to put tools on `PATH`.

---

## 5. Test README change detection

1. Edit `README.md` in `my-project` on GitHub.
2. Wait one poll cycle (default 30 s).
3. Observe a new `env_bootstrap` dispatch in logs.

---

## 6. Dashboard visibility

Bootstrap sessions appear in the coordinare dashboard under **Active Sessions** with role label `env_bootstrap` and the symphony name. They are visually distinct from regular card-work sessions.

---

## Notes

- Regular performers receive the env volume **read-only**. If a tool needs write access at runtime (e.g., pip's bytecode cache), the `env_bootstrap` image should pre-populate writable overlay paths or configure `PYTHONPYCACHEPREFIX` etc.
- Concurrent bootstraps for the same symphony are serialised. If a README changes while a bootstrap is in flight, the new bootstrap runs after the current one completes.
- No cleanup of stale cache directories on symphony removal — this is manual for now.
- SHA polling always queries `HEAD` (the repo's default branch). Changes to `env_spec_files` entries on non-default branches will not be detected.
