# Service Inference: Manual Override (`.coordinare/score.json`)

Coordinare's env-bootstrap performer runs an LLM-driven pass (spec 063) to
discover supportive services (Postgres, Redis, etc.) a project needs, render
`services-{start,stop,health}.sh`, and validate them inside the env cache. When
the LLM gets it wrong for an exotic stack — or you simply want deterministic,
reviewable output — commit a hand-authored manifest at
`<repo-root>/.coordinare/score.json`. The performer detects it, **skips the LLM
entirely**, and feeds the file straight into the same templating + validation
path.

## When to use this

- The LLM repeatedly mis-classifies your stack.
- You need a service the inference prompt has no signal for (proprietary
  binary, in-house daemon).
- You want fully deterministic env-cache builds and don't want to pay LLM
  latency on every README change.
- You're debugging an inference regression and need a known-good baseline.

## File location

```
<project-repo-root>/.coordinare/score.json
```

The filename is intentional: every project is a "score" in coordinare's
symphony/performer naming. The file lives in the project repo (alongside your
code), not in coordinare's config.

## Schema

`score.json` must validate against `ServicesManifest` (see
`src/coordinare/services/service_inference/schema.py`). Top-level shape:

```json
{
  "services": [ /* ServiceEntry, ... */ ],
  "cache_inputs": [ "relative/path", ... ],
  "agent_version": "manual-override"
}
```

- `agent_version` — Always written as `"manual-override"`. If you put anything
  else, coordinare force-overwrites it on load so downstream telemetry can
  distinguish hand-authored manifests from LLM output.
- `cache_inputs` — Project paths whose SHA drives env-cache invalidation. If
  omitted, the env-cache hashes `.coordinare/score.json` itself, so any edit
  forces a rebuild.
- `services` — Zero or more entries, each:

| Field | Type | Notes |
|---|---|---|
| `name` | string | Must match `^[a-z][a-z0-9_-]*$` — interpolated into shell var names and paths. |
| `binary` | string | Executable name (PATH-resolvable in the performer container) or absolute path. |
| `version` | string | Required version string. Free-form; surfaced in diagnostics. |
| `data_dir` | string | Filesystem path for service state. Conventionally under `$XDG_RUNTIME_DIR`. |
| `port` | int | 1–65535. |
| `why_needed` | string | One-line human explanation. Shows up in logs. |
| `sources` | list[string] | Optional repo paths that justify the entry (lint/audit signal). |
| `external_required` | bool | `true` means the service cannot run in-container; operator must supply connection env vars. |
| `required_env_vars` | list[string] | Required when `external_required=true`. Names only, no values. |
| `start_args` | list[string] \| null | Optional argv-style override for the launch command. When null the templater synthesises `<binary> --port=<port> --data-dir=<data_dir>`. Each element is shell-quoted independently — pass arguments as separate list entries, not a single shell string. |

Duplicate service names and `external_required=true` with no
`required_env_vars` are rejected.

## Worked example: Rails + Postgres

A typical Rails monolith needs Postgres for development. Here is a complete
`score.json`:

```json
{
  "services": [
    {
      "name": "postgres",
      "binary": "postgres",
      "version": "16",
      "data_dir": "/devenv/symphony/var/postgres",
      "port": 5432,
      "why_needed": "ActiveRecord primary database (config/database.yml uses pg adapter)",
      "sources": ["config/database.yml", "Gemfile.lock"],
      "external_required": false,
      "required_env_vars": [],
      "start_args": ["postgres", "-D", "/devenv/symphony/var/postgres", "-p", "5432", "-c", "unix_socket_directories=/tmp"]
    }
  ],
  "cache_inputs": [
    "Gemfile.lock",
    "config/database.yml",
    ".coordinare/score.json"
  ],
  "agent_version": "manual-override"
}
```

A few things worth calling out:

- **`start_args` is provided.** The synthesised default
  (`postgres --port=5432 --data-dir=...`) would launch postgres with the wrong
  flag spelling (`-D`, not `--data-dir`). When the binary's CLI doesn't match
  the synthesiser's convention, supply `start_args` explicitly.
- **`cache_inputs` includes `score.json` itself.** This is belt-and-suspenders:
  if you edit any of the listed files the cache invalidates; if you edit
  `score.json` directly it invalidates too.
- **`data_dir` is under `/devenv/symphony/`.** That's the in-container env
  cache root. Coordinare exposes it as the mount path; using anything else
  means service state won't persist across performer restarts.

## How coordinare applies it

When the env-bootstrap performer starts:

1. It looks for `.coordinare/score.json` in the project root.
2. If found, it reads, JSON-parses, and validates the file against
   `ServicesManifest`. **Malformed JSON or schema violations fail the build**
   with a message referencing the file path (FR-014).
3. The templater renders `services-{start,stop,health}.sh` from the manifest.
4. The same dry-run + health-check validation that wraps LLM output runs
   against the rendered scripts (FR-002). A bad manual override fails the
   build the same way a bad LLM output does.
5. Artifacts are written to `<env-cache>/services/`:
   - `services.json` — verbatim manifest (with `agent_version=manual-override`)
   - `services-start.sh`, `services-stop.sh`, `services-health.sh`

If the file is absent the performer falls through to the LLM path.

## Verifying it took effect

Two signals confirm the override was applied:

- **Structured log line** from the performer:
  ```
  service_inference.manual_override_applied path=.coordinare/score.json services=[postgres]
  ```
- **Dashboard "Service inference" row** under the symphony's env-cache panel
  shows `skipped: manual_override` (rather than an LLM agent version + attempts
  count). `Inference at` shows the timestamp of the most recent bootstrap.

Inside the container you should see:

```
$ ls /devenv/symphony/services/
services.json  services-start.sh  services-stop.sh  services-health.sh

$ cat /devenv/symphony/services/services.json | jq .agent_version
"manual-override"
```

## Iterating

Edits to `score.json` invalidate the env cache on the next bootstrap cycle
(via `cache_inputs`). To force an immediate rebuild without code changes:

1. Edit `score.json` (even a whitespace change is enough if it's in
   `cache_inputs`).
2. Commit and push.
3. Coordinare's env-cache watcher picks up the SHA change and dispatches a new
   `env_bootstrap` job.

If `services-health.sh` fails at performer runtime, coordinare marks the cache
`runtime_health_failed` and the next cycle forces a regeneration — but for a
manual override that just means re-running your own scripts. Fix the
`start_args` or `health` shape in `score.json` and commit.

## Removing the override

Delete `.coordinare/score.json` and commit. The next bootstrap finds no file,
falls through to the LLM path, and overwrites `services.json` with
`agent_version=<llm-prompt-version>`.

## Related

- Spec: `specs/063-llm-service-inference/spec.md` — FR-014/FR-015 cover this
  behaviour.
- Schema source-of-truth: `src/coordinare/services/service_inference/schema.py`.
- Apply path: `src/coordinare/services/service_inference/manual_override.py`.
