# Workspace named volume implementation plan

Goal: isolate Docker workspace writes while preserving daemon-visible output.
Spec: spec.md. No network, Docker, commits or installed-skill edits.

1. Write failing unit tests for explicit mount sources, generated workspace mounts,
   selective migration, Docker runtime/smoke arguments, host guards, installer
   consent/failure and ownership detection.
2. Extend VolumeMount; update both Docker launchers and host-path enumerators.
   Generate a stable per-config volume and selectively migrate starter workspaces.
3. Extend installer removal with deterministic naming, confirmation and nonfatal
   Docker errors. Check shell/Python naming parity with local fake commands.
4. Enforce smoke ownership assertion before fallback cleanup and clean smoke volumes.
5. Update setup, paths, upgrade, operator and removal documentation. Run relevant
   pytest suites, make lint (baseline holds) and make typecheck. Review full diff.

Review focus: invalid/ambiguous sources, named volumes entering host guards,
custom settings preserved, no-terminal uninstall, in-use volume failure,
symlinks during ownership scans, and no Docker available for local verification.

Completed locally: generation/migration, mount model and both Docker launch paths,
host readers, installer consent/failure handling, smoke ownership/volume cleanup,
and seven setup/operator/removal docs. Independent read-only review found no
remaining actionable issues after the absent-data smoke fix. Interpretation:
custom host sources at the starter target /workspace migrate because they are
workspace storage; other targets, named sources and other endpoints are preserved.

Validation: 622 relevant unit tests passed (three structlog UserWarnings),
make lint passed, Ruff baseline holds (1364 violations across 43 rules),
and make typecheck passed (318 source files). git diff --check passed.
Real Docker smoke and job ownership validation remain unrun: Docker and network
are unavailable by task constraint. No commits were made.
