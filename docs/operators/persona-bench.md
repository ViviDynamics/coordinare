# Persona bench

`scripts/persona_bench.py` dispatches one deliberately hard task per persona to a
real performer container, grades the result and sorts each cell into PASS,
FAIL_MODEL (the model got it wrong), FAIL_HARNESS (our plumbing) or ERROR.

## What a cell sends

Every `/jobs` payload is built by the production builders
(`coordinare.services.http_performer_service.build_job_payload` and
`build_env_bootstrap_payload`) from a card context shaped like the daemon's:

| Task | Dispatched like | Extra context |
| --- | --- | --- |
| assessor, architect, implementer, tech_writer, tech_writer_wiki | a card stage (`dispatch_performer`) | the role's model block, orchestration and workflow |
| reviewer, qa, closer | a card stage on a fixture PR | the sanitized PR diff, its status and the changed paths |
| security | a card stage on a fixture PR | the above, plus the mandatory scan request (`security_floor`) bound to the PR head |
| env_bootstrap | the env-cache bootstrap | the manifest and independent review from `discover_and_review`, the rendered `verify.sh` and `activate.sh`, a cache volume at `/devenv/conductor-bench` |
| advocate | the card-less intake (`intake_dispatch`) | the `advocate` workflow and its workflow env; two fixture issues opened for the run and closed after it |

For nare the model block carries `endpoint_kind` and `base_url` in metadata and
the endpoint's bearer as `OPENAI_API_KEY`, as `_inject_nare_secrets` sends it.

Each cell writes `cells/<label>__<endpoint>.json` and, beside it,
`cells/<label>__<endpoint>.raw.jsonl`: the performer's job transcript, which
holds the model's raw output before any post-processing.

## Fixtures

The fixture repository is a private copy of `conductor-bench` (its `BENCH.md`
maps the branches). These must exist on its remote:

- branches `feat/impl-failing-test`, `fix/reviewer-offbyone`,
  `bench/security-sqli-vuln`, `feat/qa-behavior`, `feat/techwriter-nodoc`,
  `feat/techwriter-wiki`, `feat/closer-ready`;
- open PRs for `fix/reviewer-offbyone`, `bench/security-sqli-vuln`,
  `feat/qa-behavior` and `feat/closer-ready`, listed in the `--prs` JSON as
  `reviewer`, `security`, `qa` and `closer`.

`scripts/bench_setup.sh <repo_git_url> [clone_dir]` checks the branches, opens
any missing PR and writes `tmp/bench_prs.json`. The security PR must be the one
for `bench/security-sqli-vuln`: the bench forks that branch and binds the scan
request to its head, and a PR on another head is refused as FAIL_HARNESS.

The GitHub token needs contents, pull request and issue write access to the
fixture repository: tasks push throwaway `bench-ro-*` branches (deleted after
each cell) and the advocate task opens and closes issues.

## Config

`--config` must be a full coordinare config, since the bench resolves models,
personas and workflows through `ProjectConfiguration`. A nare run:

```yaml
github_org: <org>
endpoints:
  - {name: gateway, kind: litellm, base_url: "https://<gateway>/v1", auth_env: LITELLM_MASTER_KEY}
model_endpoints:
  - {name: haiku, endpoint: gateway, model: <model id on the gateway>}
modes:
  - {name: solo, strategy: single, tool: haiku}
performers:
  default: {backend: nare, mode: solo}
performer_endpoints:
  - id: nare-ephemeral
    env:
      BACKEND: nare
      NARE_BASE_URL: "https://<gateway>/v1"
      OPENAI_API_KEY: "${LITELLM_MASTER_KEY}"
conducting:
  backend: openai_api
  base_url: "https://<gateway>/v1"
  api_key_env: LITELLM_MASTER_KEY
  model: <model id on the gateway>
```

`conducting` is the model for environment discovery and review, as in
production. Without an `openai_api` conducting block the bench falls back to
`--judge-model` on the inference gateway; with neither, the env_bootstrap cell
is FAIL_HARNESS.

`.env` at the repository root supplies `LITELLM_MASTER_KEY`,
`COORDINARE_INFERENCE_BASE_URL` (the judge, and the advocate workflow's
gateway when the endpoint env sets no `LITELLM_PROXY_BASE_URL`) and the GitHub
token (`GH_TOKEN` or `GITHUB_TOKEN`, else a `.gh_token` file in the parent of
the checkout).

## Run

```sh
scripts/bench_setup.sh <fixture repo clone URL> <local clone dir>
uv run python scripts/persona_bench.py \
  --repo <fixture repo clone URL> --prs tmp/bench_prs.json \
  --config <bench config.yaml> --backends nare-ephemeral \
  --judge-model <model id on the gateway> --tag <run name>
```

`--roles` takes labels or performer keys (`--roles tech_writer` runs both
tech_writer tasks). Results land in `specs/077-multi-backend-qa/persona_runs/<tag>/`.
