# Agent setup interface

The ordered procedure is [setup for agents](setup-for-agents.md).

## Init answers

`init --answers PATH` reads a YAML mapping, including JSON syntax. Unknown keys
are rejected. The [JSON Schema](schemas/init-answers.schema.json) describes its
types; the [example](examples/init-answers.yaml) contains no secrets and is
validated by the normal pytest CI suite. The schema describes recommended
values; live credentials, available models, boards and image tags still need
init's semantic and network checks. `writeOnly` marks secrets for tooling, it
does not encrypt a file. Supply them through the environment.

Precedence is flag, then `COORDINARE_INIT_*`, then answers file, then saved values
and detected defaults. Env answer values are text. Integer answers accept a
positive integer or its decimal string. Reviewers accept a list in YAML or
comma-separated text, including in environment variables; do not encode an env
reviewer list as JSON.

| Answer key | Flag | Environment answer | Meaning/default |
| --- | --- | --- | --- |
| `github_token` | `--github-token` | `COORDINARE_INIT_GITHUB_TOKEN` | Private user token; otherwise detected from `COORDINARE_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`, saved token, then `gh auth token` |
| `repository` | `--repository` | `COORDINARE_INIT_REPOSITORY` | `owner/name`; current GitHub origin can supply default |
| `project_number` | `--project-number` | `COORDINARE_INIT_PROJECT_NUMBER` | Positive board number; required unattended |
| `harness` | `--harness` | `COORDINARE_INIT_HARNESS` | `codex`, `claude_code`, `opencode`; installed CLI preferred, otherwise Codex |
| `model` | `--model` | `COORDINARE_INIT_MODEL` | Codex/OpenCode `gpt-5.4`; Claude Code `claude-sonnet-4-6` |
| `endpoint` | `--endpoint` | `COORDINARE_INIT_ENDPOINT` | Empty for native; otherwise OpenAI-compatible base URL |
| `api_key` | `--api-key` | `COORDINARE_INIT_API_KEY` | Private provider key; defaults to `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`, then saved value |
| `max_concurrent_cards` | `--max-concurrent-cards` | `COORDINARE_INIT_MAX_CONCURRENT_CARDS` | Positive integer; 1 initially |
| `human_reviewers` | `--human-reviewers` | `COORDINARE_INIT_HUMAN_REVIEWERS` | Nonempty reviewer logins; authenticated user initially |
| `run_mode` | `--run-mode` | `COORDINARE_INIT_RUN_MODE` | `local` initially, or `docker` |
| `image_variant` | `--image-variant` | `COORDINARE_INIT_IMAGE_VARIANT` | `full` initially, or `base`; Docker only |
| `image_tag` | `--image-tag` | `COORDINARE_INIT_IMAGE_TAG` | Installed release version by default; Docker only |

Other flags are `--answers PATH`, `--config-dir PATH`, `--non-interactive`,
`--force`, `--skip-smoke`, and mutually exclusive `--start` (foreground) and
`--start-at-login` (install/start user service). These have no `COORDINARE_INIT_*`
equivalents. Without a terminal init already avoids prompts. `--answers` alone
does not suppress prompts in a terminal. Init emits prose, not JSON.

Init exits 0 on success, 1 on setup or preflight failure, and 2 for invalid CLI
arguments. Setup exceptions produce `Setup failed at STAGE: CAUSE.` and a `Fix:`
line. Stages identify answers, GitHub, board, harness, Docker runtime/disk, image
pull/inspect, container setup/smoke, model endpoint, config write, preflight or service
setup. Causes use exception classes, OS errno names/system descriptions, HTTP
status codes/hosts or recognized Docker failures. Raw exception messages,
filenames, full URLs and Docker stderr are withheld. Follow the fix before retrying; `ENOSPC`
during Docker provisioning means free space on the Docker filesystem and inspect
usage with `docker system df`.

For local mode install and authenticate the chosen harness CLI on PATH (`codex`,
`claude`, or `opencode`); the release installer already bundles `performer`.
Codex/Claude Code can reuse a local harness login without an API key. OpenCode's
native provider requires an API key. Docker cannot reuse host harness logins and
requires an API key for native providers. Self-hosted endpoints can omit keys.

## JSON reports

`coordinare validate --json` aliases `coordinare config validate --json`.
Both validate schema and environment placeholders without launching the daemon.
`--strict` makes deprecation and omitted placeholder warnings fail. Doctor
supports `--config PATH`, `--json`, and repeatable `--check egress`. The egress
check is opt-in because it creates Kubernetes Pods. Do not use it during the
Docker installation procedure.

All three spellings accept `--config PATH` before or after the command; for
`config validate` it may also follow `config`. Omit it only when discovery is
intentional: explicit flag, `COORDINARE_CONFIG_PATH`, working directory
`config.yaml`, XDG user config, legacy `~/.coordinare/config.yaml`, then env only.

Schema: [setup-report.schema.json](schemas/setup-report.schema.json).

```json
{"schema_version":1,"command":"validate","ok":true,"checks":[{"id":"config.schema","instance":1,"status":"ok","message":"Configuration schema validation.","fix":null,"field":null}]}
```

`schema_version` is the protocol version. `command` is `doctor` or `validate`.
`checks` is nonempty. Each check has `id`, `instance` (positive integer), `status`,
`message`, `fix` (text or null), and `field` (known top-level config field,
`config_file` for a file problem, or null).
Status is `ok`, `fail`, `warn`, or `skip`. Every failure has a nonempty fix.
`ok` is true exactly when no check has status `fail`. Exit codes are 0 for true,
1 for false. Warnings and skips do not block doctor. Invalid command syntax
exits 2 with argparse usage, outside the JSON protocol.

IDs are stable categories. `(id, instance)` identifies a check within a report.
Instances count from 1 per id in execution order, including repeated checks for
catalog entries and enabled symphonies. They are stable for unchanged config
ordering; do not use them as persistent resource identities after editing config.
Messages and fixes may improve within schema version 1; branch on ids/statuses.
Consumers should accept additional ids. The check set varies with configuration
and early failures. JSON uses curated messages and fixes, never raw probe text,
config values, endpoint names/URLs, arbitrary field keys or exception strings.
Nested validation fields are reduced to a known top-level setting to prevent
input disclosure. The prose command retains its existing detailed output.

| Stable ids | Subject/fix area |
| --- | --- |
| `config.schema`, `config.load` | Schema outcome or config load failure |
| `config.missing`, `config.wrong_type`, `config.unknown_field`, `config.invalid_value` | Validation findings; `field` identifies a safe top-level setting when available |
| `config.deprecated`, `config.placeholder` | Deprecated fields and omitted placeholders |
| `doctor.startup` | Start-time invariants and required secrets |
| `doctor.state_directory`, `doctor.performer_directory`, `doctor.transcript_directory`, `doctor.run_log_directory` | Writable storage |
| `doctor.dashboard_port`, `doctor.dashboard_binding` | Port availability and loopback bind |
| `doctor.performer_version`, `doctor.performer_image` | Matching daemon/image and local image availability |
| `doctor.endpoint`, `doctor.model`, `doctor.native_model` | Reachability, Ollama model enumeration, native authentication |
| `doctor.github_credential`, `doctor.github_token`, `doctor.github_scopes`, `doctor.github_permissions`, `doctor.github_api`, `doctor.github_repository` | GitHub credential, permissions and repository probes |
| `doctor.board_columns`, `doctor.github_board` | Board access and Status options |
| `doctor.harness`, `doctor.container_harness`, `doctor.remote_harness` | Harness readiness by run mode |
| `doctor.performer_protocol`, `doctor.performer_transport`, `doctor.performer`, `doctor.container_runtime` | Performer protocol, transport, persistent endpoint and Docker |
| `doctor.egress`, `doctor.other` | Opt-in egress enforcement or future uncatalogued checks |
| `doctor.execution` | Unexpected preflight failure; checks could not complete |

Doctor reuses the launch preflight, with strict deterministic failures, live
network checks and temporary Docker containers. Transient connection failures,
timeouts, 429 and server errors become warnings after bounded retries. A native
model can be skipped when it cannot be enumerated; a harness-login credential
cannot be proved until first use. Neither command proves model output quality,
every write permission, or a successful daemon launch. No model inference runs.
