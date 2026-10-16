# First launch

Start from `config.minimal.yaml`, a 15 line starter. Keep `config.example.yaml`
as the full reference. Copy the starter to `config.yaml` and edit the GitHub owner,
repository name, project number and human reviewer logins. Pick one harness:
`codex`, `claude_code` or `opencode`.

Install the harness CLI and authenticate it. The starter uses provider API keys:
export `COORDINARE_GITHUB_TOKEN` and `OPENAI_API_KEY` for Codex or OpenCode, or
`ANTHROPIC_API_KEY` for Claude Code. Subscription-only harness login is a separate
advanced configuration; it does not satisfy a preset's API-key model endpoint.
Use `uv sync --project agent/performer` in a checkout to install the performer.
For packaged installs, install the matching performer package and put its
`performer` executable on PATH.

Create a GitHub Project Status field with To Do (or Ready), In Progress, In Review,
Blocked and Done. With a classic PAT, grant `repo`, `project` and `read:org` scopes
and authorize organization SSO if required. Fine-grained tokens and Apps need
repository Contents, Issues and Pull requests read/write and organization Projects
read/write. Preflight proves read access and classic scopes; write authorization
and branch protection remain GitHub's responsibility at the time of a write.

```sh
set -a && source .env && set +a
python -m coordinare config validate
python -m coordinare config
python -m coordinare doctor
python -m coordinare
```

Doctor and daemon startup share their checks. Deterministic failures block a
preset launch, including rejected credentials, missing repositories or boards,
missing lifecycle columns and missing local executables. Existing full configs
without a preset receive warnings for the new checks, including missing classic
PAT scopes and an absent Blocked column. Existing configuration validation and
runtime requirements still apply.

Connection failures, timeouts, HTTP 429 and server errors receive a short bounded
retry and then WARN. They do not stop startup. GitHub initialization also defers
transient failures to board polling, so the running daemon can recover and its
System health panel reflects the outage. Each HTTP attempt has a five second
timeout, with at most two attempts and a 0.1 second retry delay. Every warning or
refusal names the setting or command to fix. `coordinare doctor` displays all
configured checks, including unused model catalogs and repository access, and
returns nonzero for deterministic failures. Setting `github_repo` alone in a full
config does not enable the repository probe at startup.

Temporary container harness probes run only for a preset's first launch, when
the state file does not yet exist, or when doctor is run explicitly. Container
CLI installation can take up to the endpoint's `readiness_timeout_s` (300 seconds
per attempt by default). A working installed Codex version is reused without an
npm install. Metadata probes do not generate model output or spend inference
tokens. Container probes start temporary containers without credentials or
repository mounts and remove them afterward.

`preset: solo` runs the performer locally, uses one card slot and enables only
implementer. The performer protocol executable defaults to the installed
`performer`, or `bin/run-performer` in a checkout. The daemon passes the chosen
backend and model through the existing dispatch protocol.

`preset: containers` uses the same lifecycle with ephemeral Docker performers.
Run `make docker` first or set `performer_image` to an available matching image.
Explicit fields override presets. Nested mappings merge; lists replace defaults.
When replacing a catalog without keeping its `starter` entry, override
`endpoints`, `model_endpoints` and `modes` together, and set
`performers.default.mode` to your mode name. Validation names these linked settings
if a partial replacement would leave starter references behind.
`performers.<role>: null` disables a role. A config without a preset keeps its
existing behavior. Use the full reference to add roles or self-hosted models.

## Try one card

Create an issue in your configured repository:

Title: **Document our first Coordinare run**

Body:

> Create FIRST_CARD.md at the repository root with one sentence: Coordinare can
> open a pull request for this repository. Change no other files. Open a pull
> request for a human to review.

Add the issue to your Project and set its Status to To Do or Ready. If you use
`assignee_filter`, assign that login; if you use `include_unassigned`, leave it
unassigned. Watch the dashboard show In Progress and then an open PR in In Review.
Review it as one of `human_reviewers`. Existing approval and required-check gates
still govern merging. The dashboard's **Try a first card** disclosure offers the
same example when the board is empty, without creating an issue automatically.
Blocked cards retain their idle explanation without the first card prompt.

The next setup command in #195 can reuse `coordinare.presets.expand_preset`,
`coordinare.launch_preflight.run_preflight` and `coordinare.first_launch.sample_issue`.
