# Implementation Plan: Docker/Compose Runtime Entry and Visibility

**Branch**: `002-docker-cli-output` | **Date**: 2026-02-16 | **Spec**: [~/Workspaces/ViviDynamics/coordinare/specs/002-docker-cli-output/spec.md](~/Workspaces/ViviDynamics/coordinare/specs/002-docker-cli-output/spec.md)
**Input**: Feature specification from `/specs/002-docker-cli-output/spec.md`

## Summary

Add two supported runtime entry paths for the coordinare daemon: (1) direct shell-script execution without Docker and (2) Docker Compose execution with project-contained Docker artifacts. Ensure runtime output remains understandable in both modes, with required Docker Compose/config templates present in the repository while local configuration instances are excluded via `.gitignore` and `.dockerignore`.

## Technical Context

**Language/Version**: Python 3.12  
**Primary Dependencies**: Existing coordinare Python runtime dependencies from `pyproject.toml`; Docker; Docker Compose  
**Storage**: File-based runtime configuration (YAML + environment variables), no new database for this feature  
**Testing**: pytest (smoke + integration checks), ruff, mypy  
**Target Platform**: Linux shell environments and Linux Docker hosts  
**Project Type**: Single backend/daemon project  
**Performance Goals**: Startup status visible within 10 seconds; heartbeat/major event visibility during runtime in both execution modes  
**Constraints**: Non-zero exit on runtime errors; human-readable default logs with optional structured mode; known sensitive fields redacted  
**Scale/Scope**: Single daemon instance per compose project; local/dev and single-host deployment workflows

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

### Pre-Research Gate Review

- **I. Code Quality First**: PASS — scope is operational runtime packaging and entrypoint clarity with minimal additional complexity.
- **II. Testing Discipline**: PASS — plan includes shell/compose smoke verification and runtime behavior assertions.
- **III. User Experience Consistency**: PASS — output semantics are defined to stay equivalent across runtime modes.
- **IV. Performance by Design**: PASS — startup visibility and runtime observability targets are measurable in spec success criteria.
- **V. Clarity Before Action**: PASS — no unresolved clarifications remain in spec.

**Gate Status (Pre-Research)**: PASS

### Post-Design Gate Review

- **I. Code Quality First**: PASS — design artifacts are cohesive and map directly to spec requirements.
- **II. Testing Discipline**: PASS — contracts and quickstart define testable acceptance paths for both run modes.
- **III. User Experience Consistency**: PASS — same lifecycle output taxonomy retained across shell and compose.
- **IV. Performance by Design**: PASS — design does not introduce conflicting runtime constraints.
- **V. Clarity Before Action**: PASS — all technical choices resolved without placeholder ambiguity.

**Gate Status (Post-Design)**: PASS

## Project Structure

### Documentation (this feature)

```text
specs/002-docker-cli-output/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── runtime-openapi.yaml
│   ├── shell-runtime-contract.md
│   └── compose-runtime-contract.md
└── tasks.md
```

### Source Code (repository root)

```text
src/
└── coordinare/
   ├── __main__.py
   ├── daemon.py
   ├── config.py
   ├── lib/
   │  ├── runtime_events.py
   │  └── redaction.py
   └── ...

scripts/
└── run-coordinare.sh                # shell runtime entrypoint (planned)

Dockerfile                          # image build artifact (planned)
docker-compose.yml                  # compose runtime definition (planned)
config.example.yaml                 # committed template (planned)
.env.example                        # committed env template (planned)

.gitignore                          # excludes local config instances
.dockerignore                       # excludes local config instances from build context

tests/
├── unit/
├── integration/
└── contract/
```

**Structure Decision**: Keep the existing single-project daemon layout and add top-level operational artifacts for runtime packaging (`Dockerfile`, `docker-compose.yml`, config templates) and script-based execution (`scripts/run-coordinare.sh`).

## Complexity Tracking

> No constitution violations requiring justification.

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|--------------------------------------|
| *(none)* | — | — |
