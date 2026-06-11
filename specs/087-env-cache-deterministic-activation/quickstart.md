# Quickstart: Verifying Deterministic Env-Cache Native-Lib Activation

## What changed

`agent/performer/devenv-profile.sh` (baked into the performer image) now, for each mounted
env cache that ships captured `.deb` packages, extracts their shared objects to a writable
per-cache lib dir and prepends it to `LD_LIBRARY_PATH` — before sourcing the cache's
`activate.sh`. This makes native runtimes (e.g. Ruby linked against `libyaml`) loadable
without depending on the LLM-authored `activate.sh`.

## Run the shell-level unit tests (primary verification)

```sh
.venv/bin/pytest tests/unit/test_devenv_profile_shell.py -v
```

These build a fake `/devenv` tree (with a synthetic `.deb` carrying a `.so`) and source the
profile script under bash and dash, asserting contracts C1–C9.

## Manual end-to-end check on a performer container (optional, read-only)

> Only inspect coordinare's own `coordinare-performer:full` containers. Do not touch the
> an in-house project `client-test-*` / `authentication-development-*` / inhouse-* stacks.

1. Identify a running QA performer with a Ruby cache mounted at `/devenv/<slug>/`.
2. Confirm the cache carries the debs:
   ```sh
   docker exec <ctr> sh -c 'ls /devenv/*/debs/ | grep -i libyaml'
   ```
3. In a fresh shell (which sources the profile), confirm the lib dir + linker path:
   ```sh
   docker exec <ctr> bash -lc 'echo "$LD_LIBRARY_PATH"; ruby -ryaml -e "puts RUBY_VERSION"'
   ```
   Expect: the per-cache lib dir present in `LD_LIBRARY_PATH` and Ruby printing its version
   (no `libyaml-0.so.2: cannot open shared object file`).
4. Confirm one-time extraction (sentinel present, no re-extract cost):
   ```sh
   docker exec <ctr> sh -c 'ls -a /var/lib/devenv/*/ | grep extracted'
   ```

## Success signals

- `ruby -ryaml -e 'true'` / `require "psych"` exits 0 (SC-001).
- A Ruby QA run produces a real test pass/fail verdict instead of "environment/deps
  missing" (SC-002).
- Holds even with today's no-deb-handling `activate.sh` (SC-003).
- `sh -c` and `bash -c` see the same `PATH`/`LD_LIBRARY_PATH` (SC-004).
- No shell aborts / bootstrap deadlocks attributable to the profile script (SC-005).
```
