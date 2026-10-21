# Generated credentials for disposable local services

Issue #353; PR #354. Status: written proposal for maintainer review.

## Why

The GLM Elm attempt (`source-evidence-cov8-internal-glm53flash-elm60m-high32k-v2`)
held with `attempt_schema_limit` during manifest construction. Its checks start a
local PostgreSQL service and connect to it with the throwaway passwords the
repository's CI uses (`postgres/postgres`, `financepass`, role passwords for the
Rails suites). The manifest validator treats every credential-shaped value as a
runtime secret:

- an `env` key matching `token|secret|password|credential|api.?key` must have the
  value `${KEY}`;
- a URL with a user and password in `argv` or `env` must use `${NAME}` as the
  password.

A `${NAME}` reference resolves only from operator-supplied secrets
(`environment_secret_names`, carried by `COORDINARE_ENV_SECRET_VALUES`), and
`_expand` raises on an unresolved name. No operator secret exists for a database
the manifest itself creates. So any repository whose tests need a local database
with a password can never produce a valid manifest. GLM spent its last three
schema repairs moving the literal between `env` and `argv`.

The rule against literal credentials is right and stays. What is missing is a
trusted source of values for credentials that only exist inside the disposable
environment.

## Goals

- A manifest can name credentials for services it declares and starts, and use
  them only as `${NAME}`.
- The trusted runner generates the values. They never appear in the manifest, the
  scope contract, the journal, chat transcripts, check output or any persisted
  artifact.
- Readiness meaning is unchanged: every required check and service lifecycle step
  still runs and passes with the generated values.

## Non-goals and anti-goals

- No literal credentials anywhere in a manifest, including scoped exemptions.
- No change to operator-supplied runtime secrets, their transport or redaction.
- A generated name must never shadow or stand in for an operator secret. A
  manifest must not be able to satisfy a credential the source says comes from
  outside (a cloud key, a registry token) by declaring it as generated.

## Decisions (proposed)

### Declaration

`EnvironmentManifestProposal` gains `local_credentials`: a list (at most 32) of
`{name, service, purpose}`.

- `name` is an environment variable name, not reserved (`COORDINARE_ENV_*`,
  `BASH_ENV`, `ENV`, `PATH`, `COORDINARE_REPO_DIR`), and unique.
- `service` must be the `id` of a service in the same manifest. This ties each
  generated credential to something the environment itself starts.
- `purpose` is short text for reviewers (for example "PostgreSQL superuser
  password for the test database").

The daemon carries the list into `EnvironmentManifest` unchanged, so it is part
of the manifest digest.

### Validation

- `${NAME}` for a credential-shaped key or URL password is valid when `NAME` is
  either an operator-supplied secret name or a declared local credential. The
  existing `${KEY}`-must-match-its-own-key rule for credential-shaped env keys is
  kept, so a check sets `PGPASSWORD: ${PGPASSWORD}` with `PGPASSWORD` declared.
- A name may not be both operator-supplied and declared local
  (`local_credential_conflict`).
- A declared local credential that nothing references is rejected
  (`local_credential_unused`), so the list cannot grow into noise.
- Repair guidance for `runtime_credential_reference` names the new option: when
  the credential belongs to a service the manifest starts, declare it in
  `local_credentials` and reference it as `${NAME}`; never move the literal.

### Generation and lifetime

- The runner generates each value once per runner invocation with `secrets`
  (URL-safe, 32 bytes of entropy), before activation, and adds it to the process
  environment the same way supplied secrets are restored. The values are added to
  the redaction set, so they never appear in captured output.
- Because the values change on every run, a service that stores a credential
  must initialize its data in `{scratch}`, not `{cache}`. Install commands and
  activation (which also applies to install commands) cannot reference a local
  credential (`local_credential_in_install`), so nothing installed into the cache
  depends on one. Services and checks in the install pass receive that pass's
  values, so (maintainer decision 2026-10-21) any service or check command that
  references a local credential may not mention `{cache}` in its argv, cwd or
  any env value other than `PATH` (`local_credential_cache_data`). Cached tools
  are reached through activation path entries, and credential-bearing data stays
  under `{scratch}`.
- A generated name may not shadow an operator secret, an inherited variable, a
  runtime-provided variable, or a runtime-owned name (`DOCKER_HOST`,
  `SSH_AUTH_SOCK` and the rest of `RUNTIME_OWNED_ENV`, plus
  `COORDINARE_SCRATCH_DIR`). `purpose` is screened like any other text for
  credential-bearing URLs.
- A performer job generates one set of values and uses it for its own
  verification, the agent's activation and the held services. The values are
  registered with the job transcript's exact-value redaction.
- The clean consumer and every later performer run generate fresh values, so the
  digest-bound manifest works offline with no stored secret.

### Review

Executable coverage reviewers see `local_credentials` and judge whether each one
belongs to a service the environment starts. A credential the source shows comes
from outside the environment is a blocking coverage finding, not something to
generate.

## Left to the builder

- Exact error copy and guidance text, following the existing style.
- Where in `runner.py` generation happens, as long as it precedes activation,
  services and checks, and uses the existing redaction path.
- Tests: validation codes, redaction of generated values in check output, scratch
  initialization across two runs with different values, and a regression built
  from the Elm manifest shape (local PostgreSQL with role passwords).

## Delivery

Maintainer approval, then a short plan, native implementation, independent
review, preflight, CI, then a fresh GLM Elm attempt on the internal endpoint with
the same controls.
