# Contributing to coordinare

Short version: **issues yes, pull requests no.** Here is why, and what actually
helps.

## We want your issues

Bug reports, feature requests, and general feedback are genuinely welcome, and
they shape what gets built. Coordinare is developed by a small team, and the
fastest way for us to learn that something is broken or missing is for you to
tell us.

[Open an issue.](https://github.com/ViviDynamics/coordinare/issues/new/choose)
There are separate forms for bugs, feature requests, and open-ended feedback.

When you open an issue it is forwarded to the maintainers directly, so nothing
depends on someone happening to be watching the repository that day. That is a
statement about delivery, not about response: see below.

Submissions are read, and they inform the roadmap. We are not going to promise
you a reply, a triage decision, or that any particular request gets built. What
you send genuinely influences the direction; it does not obligate us to
anything, and pretending otherwise would just set you up for disappointment.

### What makes a report useful

For a **bug**, the things that save us the most time:

- What you expected, and what happened instead.
- The coordinare version or commit, plus how you are running it (local, Docker).
- Which performer roles and which model backend were involved. Coordinare is a
  multi-agent system, and "the implementer failed" and "the assessor failed" are
  usually unrelated problems.
- Relevant log output. Structured log lines with the card or session identifier
  are worth more than a screenshot of a terminal.
- Whether it reproduces, or happened once.

For a **feature request**, tell us the problem before the solution. "I cannot
tell whether a stuck card is working or wedged" is more actionable than "add a
status column", because we may already have a better answer to the first.

For **feedback**, no format needed. Tell us what confused you, what you expected
to exist, or where you gave up. Where people abandon the setup is some of the
most valuable information we get.

## We do not accept pull requests

Public pull requests are **not accepted** and are closed unmerged. This is not a
comment on your code, and it is not about wanting control for its own sake.

Coordinare is an autonomous agent system that holds GitHub write credentials,
executes AI-generated code, and feeds public text into model prompts. Merging
code from outside the organization would put a supply-chain path straight
through the middle of that. Keeping the supply chain closed removes an entire
class of attack, and the cost is that we cannot take your patch.

So all implementation happens inside the organization. If you have found a bug
and you know the fix, put the fix in the issue. Describing the change in prose,
or pasting a diff into the issue body, is genuinely useful and we will credit
you for it. We just will not merge the branch.

There is no contributor license agreement to sign, because there are no outside
contributions to license.

## Security issues

Do not open a public issue for a vulnerability. See
[SECURITY.md](SECURITY.md) for the private route.

## Licensing

Coordinare is source-available under the [Elastic License 2.0](LICENSE). You may
run, modify, and self-host it, including commercially. You may not offer it to
third parties as a hosted or managed service. If that is what you want to do,
[talk to us](https://vividynamics.com/contact) about commercial terms.

## Developing Coordinare

This checkout workflow is for maintainers changing the source. To run a release,
use the [Quick Start](README.md#quick-start). Public pull requests remain closed
under the policy above.

Install Python 3.12 or newer, uv, git and Node 22 LTS. Clone and install both
packages into the checkout environment:

```sh
git clone https://github.com/ViviDynamics/coordinare.git
cd coordinare
uv sync --extra dev
uv pip install -e 'agent/performer[dev]'
make help
```

Install one supported harness CLI on PATH. For a checkout deployment, copy the
starter and environment template, then edit their GitHub, reviewer and credential
values. The starter enables only implementer and defaults to Codex. Other starter
harness names are `claude_code` and `opencode`. The copied starter uses a model API
key; guided init can also generate a setup using a local Codex or Claude login.
Use `config.example.yaml` as the complete configuration reference.

```sh
cp config.minimal.yaml config.yaml
cp .env.example .env
```

Before running the daemon, read [developer onboarding](docs/onboarding/README.md).
Source `.env` before direct validation or daemon commands so `${VAR}` references
resolve correctly. The Makefile run targets load it for you:

```sh
set -a && source .env && set +a
.venv/bin/python -m coordinare --config config.yaml config validate
.venv/bin/python -m coordinare --config config.yaml doctor
make run
```

Open <http://127.0.0.1:8090>. `bin/run-coordinare` uses the checkout's config and
loads its `.env`; `bin/run-performer` uses the performer project environment.
For a checkout login service, use `make install` and `make uninstall`.
For Kubernetes or Helm deployment, follow the
[chart README](deploy/helm/coordinare/README.md).

The top-level Makefile is the canonical command entry point:

```sh
make test
make test-all
make test-js
make lint
make typecheck
make ci
```

Run `make test-all` before pushing. `make ci` delegates to `bin/build --all` and
includes browser tests and Docker builds. `bin/build` installs both packages'
dev dependencies, then runs lint, type checking, the ruff ratchet, tests and
coverage. `bin/build --e2e` adds browser tests; `bin/build --docker` adds Docker
builds. These require their corresponding runtimes.

Source layout: `src/coordinare` contains the daemon, graph, services and dashboard;
`agent/performer` contains the wire protocol, harness adapters and workspaces.
`tests` contains daemon tests, and `specs` contains feature specifications.
