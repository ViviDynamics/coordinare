# 343: env-bootstrap job dies with "TypeError: all environment values must be bytes or str"

## Objective

Ship GitHub issue #343: an env-bootstrap job (role env_bootstrap, backend codex) for the
`dettmore-platform` symphony fails deterministically. The daemon's `GET /jobs/{id}` poll
returns a job whose result summary is the plain text
`TypeError: all environment values must be bytes or str` instead of the serialized
PerformerResponse JSON, so the daemon logs `http_performer.job_result_malformed_json`
(`parse_error='Expecting value: line 1 column 1 (char 0)'`).

## Root cause (verified by reproduction)

1. The performer HTTP server boots with `uvicorn.run(app, ...)` (performer main.py
   `_run_server`). uvicorn's default `loop="auto"` picks **uvloop** because
   `uvicorn[standard]` (a direct performer dependency) installs it. The whole job,
   including every `asyncio.create_subprocess_exec`, runs on a uvloop event loop.
2. uvloop, unlike the stdlib event loop, rejects non-str subprocess env values at spawn
   time with exactly `TypeError: all environment values must be bytes or str`
   (uvloop/handles/process.pyx:322). The stdlib loop accepts `os.PathLike` values, so
   the same code passes on a dev machine and in every test that runs on the stdlib loop.
3. `run_env_cache_verify` (agent/performer/src/performer/workspace.py) builds the spawn
   env and adds `env["COORDINARE_REPO_DIR"] = cwd`. Both callers pass the checkout as
   `cwd`: `BootstrapEnvironment.run` step "verify" (`cwd=self.stand.path`) and
   `_bootstrap_verify_response` (`cwd=perf.stand.path`). `Stand.path` is a `Path`
   (performer models.py), so the env value handed to uvloop is a `Path`, and the spawn
   raises.
4. The TypeError escapes `run_env_cache_verify`'s `except (OSError, TimeoutError)`
   handlers, escapes the workflow's `except (ValueError, OSError)`, is caught by the job
   runner's generic handler, and becomes the job's failure summary. Deterministic: every
   env_bootstrap job that reaches the verify step on chart 2026.10.171 dies.

Sequence in the failing job: activation and the codex turn succeed (their spawn envs are
all str), inference runs its many agent iterations, then the readiness step passes and
the verify step crashes at spawn before the script can run.

## Fix (agent/performer/src/performer/workspace.py)

- Root cause: `env["COORDINARE_REPO_DIR"] = str(cwd)`.
- Defensive boundary: new `_stringify_env` helper coerces every env key and value to a
  plain str (str passthrough, bytes decoded, os.PathLike via `os.fspath`, anything else
  via `str()`), applied at the three env-merge spawn sites:
  `_start_env_cache_services`, the readiness health-retry env, and `run_env_cache_verify`.
  A stray non-str can no longer kill a job, whatever event loop runs underneath.

## Verification

- New regression test `agent/performer/tests/unit/test_343_verify_env_uvloop.py`:
  - `test_verify_with_path_cwd_passes_on_a_uvloop` runs `run_env_cache_verify` with a
    `Path` cwd on a real uvloop and reproduces the issue TypeError before the fix
    (confirmed: `uvloop/handles/process.pyx:322`), passes after it.
  - Two boundary tests assert every env value the workspace spawns hand to asyncio is a
    plain str (verify + services-start paths).
- Full performer unit suite: 3115 passed. Lint clean, ruff baseline holds.

## Out of scope

- uvloop loop selection (`loop="asyncio"`) was rejected: the stdlib loop hides the bug
  class instead of fixing it, and uvloop is the performer's throughput posture.
- Other spawn sites (backends, git helpers) keep their existing str-only inputs; the
  boundary helper covers the cache_env merge class where the bug lived.
