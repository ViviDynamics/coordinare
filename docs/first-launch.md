# First launch

Use the [Quick Start](quickstart.md) to install a release and run `coordinare init`.
Init writes configuration and private credentials, checks the setup and offers a
login service or foreground start. The installer already bundles the performer.
Local Codex and Claude Code can use an existing harness login when no API key is
supplied; OpenCode's native provider needs `OPENAI_API_KEY`. Docker native
providers need an API key because host harness logins are not mounted.

Create a GitHub Project Status field with To Do (or Ready), In Progress, In Review,
Blocked and Done. With a classic PAT, grant `repo`, `project` and `read:org` scopes
and authorize organization SSO if required. Fine-grained tokens and Apps need
repository Contents, Issues and Pull requests read/write and organization Projects
read/write. Preflight proves read access and classic scopes; write authorization
and branch protection remain GitHub's responsibility at the time of a write.

To inspect the generated setup, stop any running daemon to free the dashboard
port, then run from a directory without an old `config.yaml`:

```sh
coordinare config validate
coordinare config
coordinare doctor
coordinare
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

Outside init, temporary container harness probes run only for a preset's first launch, when
the state file does not yet exist, or when doctor is run explicitly. Container
CLI installation can take up to the endpoint's `readiness_timeout_s` (300 seconds
per attempt by default). A working installed Codex version is reused without an
npm install. Metadata probes do not generate model output or spend inference
tokens. Container probes start temporary containers without credentials or
repository mounts and remove them afterward. Init's Docker setup uses a separate
smoke check with credentials to verify GitHub and model reachability, before
writing setup files; see [Docker setup](docker-mode-setup.md).

`preset: solo` runs the performer locally, uses one card slot and enables only
implementer. The performer protocol executable defaults to the installed
`performer`, or `bin/run-performer` in a checkout. The daemon passes the chosen
backend and model through the existing dispatch protocol.

`preset: containers` uses the same lifecycle with ephemeral Docker performers.
Use `coordinare init --run-mode docker` to pull and pin the installed release's
image and configure mounts and secret delivery. See [Docker setup](docker-mode-setup.md).
An existing manually configured containers preset retains its local fallback
image unless you set `performer_image`; building custom images is a checkout
workflow described in [CONTRIBUTING](../CONTRIBUTING.md#developing-coordinare).
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
