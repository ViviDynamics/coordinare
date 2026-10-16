# Release installer

Provide a POSIX shell installer for Linux and macOS on x86_64 and arm64.
Resolve latest from its checksum manifest or honor a pinned release tag, including
legacy tags whose wheel versions normalize numeric components.

Verify all five release wheels against a strictly parsed SHA256SUMS before
bootstrapping uv or installing packages. Install the daemon and local performer in
one isolated uv tool environment under HOME, overriding shared package URLs with
the verified local wheels. External dependencies retain normal uv resolution.

Support version, yes, no-init, uninstall and purge flags and matching COORDINARE
environment settings. Reinstallation preserves configuration and state. Uninstall
preserves them unless purge explicitly removes ~/.coordinare. Custom configuration
and project state paths remain operator managed. Missing uv requires consent via
/dev/tty or --yes and uses Astral's official installer without editing profiles.

Operator direction overrides automatic init in the proposal: always print
`coordinare init` as the next step; do not implement or invoke issue #195.
Report the installed version, locations, PATH instruction when needed, and retained
or purged data. No sudo, credentials, daemon launch or configuration writes.

Use a function invoked on the last line to prevent execution of truncated bodies.
Document pipe installation and the download, inspect, run alternative. Publish the
script through the existing release asset hook, checksums and attestations.

Validation: deterministic local file release fixtures under sh, dash and bash;
corruption and malformed inventory refusal before uv invocation; pin precedence;
rerun and upgrade preservation; uninstall and purge; unattended and missing uv;
unsupported platforms; shellcheck when available. Fake release runs have a
30 second timeout. Real release installation and version smoke checks run on
Ubuntu and macOS before publication.
