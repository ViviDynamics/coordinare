# Research: Docker/Compose Runtime Entry and Visibility

**Feature**: 002-docker-cli-output  
**Date**: 2026-02-16  
**Status**: Complete

## R1: Non-Docker Runtime Entry

**Decision**: Provide a committed shell entry script at `scripts/run-coordinare.sh` as the canonical non-Docker runtime path.

**Rationale**: Scripted invocation gives a stable, repeatable command surface for developers and operators and satisfies explicit non-Docker run requirements.

**Alternatives considered**:
- Direct `python -m coordinare` only: rejected because the requirement asks for shell-script execution as a first-class path.

## R2: Dockerized Runtime Entry

**Decision**: Provide both `Dockerfile` and `docker-compose.yml` in the project root, with a compose service that runs the same application entry behavior as shell mode.

**Rationale**: Compose captures runtime wiring (environment, volumes, ports) in versioned project artifacts and aligns with requested run path.

**Alternatives considered**:
- `docker run` command snippets only: rejected because compose artifact is explicitly requested.
- Divergent container entrypoint behavior: rejected due to observability inconsistency risk.

## R3: Configuration Asset Strategy

**Decision**: Commit `config.example.yaml` and `.env.example`, while ignoring concrete local config files/secrets in both `.gitignore` and `.dockerignore`.

**Rationale**: Keeps onboarding simple without leaking environment-specific or sensitive values into source control or Docker build contexts.

**Alternatives considered**:
- Committing local runtime configs: rejected for security and portability reasons.
- Ignoring only in git: rejected because Docker context could still include sensitive files.

## R4: Runtime Output Semantics

**Decision**: Maintain shared output semantics across shell and compose modes: startup progress, major events, heartbeat summaries, state transitions, and explicit failure context.

**Rationale**: Users should not need to relearn observability patterns when switching execution mode.

**Alternatives considered**:
- Different output formats per mode: rejected because it increases support burden and validation complexity.

## R5: Output Mode and Detail Levels

**Decision**: Default to human-readable logs, optionally support structured output, and allow high-granularity details through higher log levels.

**Rationale**: Balances usability for humans and optional machine parsing/troubleshooting depth.

**Alternatives considered**:
- Structured-only: rejected due to weaker day-to-day readability.
- Always high-granularity: rejected due to noisy logs.

## R6: Failure Exit Behavior

**Decision**: Exit with non-zero status on runtime errors.

**Rationale**: Matches clarified requirement and allows unambiguous automation/health behavior in scripts and compose.

**Alternatives considered**:
- Continue running with retries after runtime errors: rejected because it conflicts with clarified requirement.

## R7: Validation Focus

**Decision**: Validate with smoke-level runtime checks for both shell script and compose flows plus output-contract checks.

**Rationale**: This feature’s value is operational execution and observability rather than algorithmic complexity.

**Alternatives considered**:
- Unit-only verification: rejected as insufficient to prove runnable entrypath behavior.
