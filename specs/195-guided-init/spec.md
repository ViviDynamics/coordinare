# Guided setup

`coordinare init` writes a small preset configuration for a first launch without
making a model call. The supplied issue is the design brief. Existing preset
expansion and launch preflight remain authoritative.

## Requirements

1. Collect token, repository, project, harness, model or endpoint, provider key,
   concurrency, reviewers and local or Docker run mode. Show defaults, prefer
   detected tools and credentials, and validate each external dependency at its
   question. Reuse preflight checks for scopes, board columns and harnesses.
2. Flags override setup environment variables, then an answers YAML mapping,
   then existing configuration and detected defaults. No terminal means no input
   calls. Report all missing required answers together.
3. Save config under XDG_CONFIG_HOME/coordinare, falling back to
   ~/.config/coordinare. Save state and logs under XDG_DATA_HOME/coordinare,
   falling back to ~/.local/share/coordinare. Discover this configuration after
   explicit, environment and checkout paths, preserving the legacy fallback.
4. Store secrets only in adjacent coordinare.env at mode 0600. Load that file
   before config expansion, with exported environment values taking precedence.
   Never include secrets in diagnostic messages or YAML values.
5. Refuse any existing output without --force. With --force keep backups and
   preserve unrelated existing settings. Validate before writing, then run and
   render the shared preflight. A failed preflight returns nonzero and keeps the
   files for repair. Print a start command and dashboard URL. Mention service
   installation as optional future functionality; Docker uses the containers
   preset, with image provisioning delegated to issue 197.

## Acceptance

Tests exercise all three preset harnesses and both run modes, answers without a
terminal, stubbed prompts, missing answers, failures with fixes, overwrite and
backup behavior, secret redaction, file permissions and discovery precedence.
Real GitHub credentials, actual harness installations and daemon startup are
operator smoke checks outside the socket-restricted development sandbox.
