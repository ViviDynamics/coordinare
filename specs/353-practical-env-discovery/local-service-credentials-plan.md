# Generated local service credentials: implementation plan

Implements [local-service-credentials-amendment.md](local-service-credentials-amendment.md)
(approved 2026-10-21, with targeted GLM repair guidance added). Execution: native,
then one independent whole-branch review.

## Global constraints

- No literal credential is ever accepted in a manifest or scope; the existing
  `reject_literal_credentials` and credential-key rules stay unchanged.
- Manifests without `local_credentials` keep their exact digest (the field is
  omitted from serialization when empty).
- Generated values are never written to the manifest, journal, chat transcripts,
  check output or any file.

## Task 1: model and validation

Files: `packages/service_inference/.../environment/models.py`, `src/coordinare/services/environment_admission.py`.
Test: `tests/unit/test_353_local_credentials_contract.py`.

- `LocalCredential(StrictModel)`: `name` (env name pattern, not `COORDINARE_ENV_*`,
  `BASH_ENV`, `ENV`, `PATH`, `COORDINARE_REPO_DIR`), `service` (min length 1),
  `purpose` (1-400 chars).
- `local_credentials: list[LocalCredential] = Field(default_factory=list, max_length=32)`
  on `EnvironmentManifestProposal` and `EnvironmentManifest`; the manifest
  serializer pops it when empty.
- `EnvironmentManifest.validate_contract` raises `PydanticCustomError` codes:
  `local_credential_invalid` (duplicate name, or unknown service id),
  `local_credential_unused` (no `${NAME}` in any non-install command, service
  command or activation), `local_credential_in_install` (an install command
  references one).
- Admission maps the three codes with guidance; `runtime_credential_reference`
  guidance names `local_credentials` for credentials of services the manifest
  starts, and in the scope stage says to describe credential-bearing URLs without
  the password.
- Tests: Elm-shaped manifest (local PostgreSQL, `PGPASSWORD: ${PGPASSWORD}`,
  URL `postgres://postgres:${PGPASSWORD}@localhost`) validates; each code fires;
  an empty list leaves an existing fixture manifest's digest unchanged.

## Task 2: generation, injection and redaction

Files: `.../environment/runner.py`, `.../environment/runtime_execution.py`,
`agent/performer/src/performer/environment_contract.py`.
Test: `tests/unit/test_353_local_credentials_runner.py`.

- `local_credential_values(manifest, *, supplied: Collection[str]) -> dict[str, str]`
  in `runner.py`: `secrets.token_urlsafe(32)` per declared name; raises
  `ValueError('local_credential_conflict')` when a name is also an operator secret.
- `credentials: Mapping[str, str] | None = None` keyword threads through
  `manifest_environment`, `manifest_services`, `_template_preflight`,
  `_run_manifest_commands` and `execute_with_runtime`. `manifest_environment`
  applies the values before activation and lists their names in
  `COORDINARE_ENV_SECRET_NAMES`, so `_redact` hides them; command redaction sets
  include the values.
- `run_manifest` generates once per invocation; the performer stand generates once
  and passes the same values to `manifest_environment` and `manifest_services`.
- Tests: a check that echoes `$PGPASSWORD` passes and its output shows
  `[REDACTED]`; two runs get different values; a conflicting operator secret
  fails the run before any command executes; the manifest JSON never contains a
  generated value.

## Task 3: review and scope guidance

Files: `src/coordinare/services/environment_discovery.py` (coverage reviewer and
manifest author instructions), `environment_admission.py`.
Test: `tests/unit/test_353_local_credentials_guidance.py`.

- Author instruction: credentials for services the manifest starts go in
  `local_credentials` and are referenced as `${NAME}`; data that stores them lives
  in `{scratch}`.
- Reviewer instruction: judge each local credential; one the source shows comes
  from outside the environment is a blocking coverage finding.
- Scope guidance for `extra_forbidden` at `scope/finding_responses`: return
  `finding_responses` beside `scope`, never inside it.
- Tests assert the guidance appears for the Elm error shapes seen in the run.

## Delivery

Full unit suite, preflight, push, CI, independent review, then a fresh GLM Elm
attempt with the same controls.
