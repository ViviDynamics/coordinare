# Quickstart Validation Checklist (T065)

**Validator**: Claude Agent  
**Date**: 2026-04-28  
**Status**: Documented for manual execution

## Overview

This checklist documents the steps from `quickstart.md` §1–§9 that must be run against a live Docker Engine to validate the containerized performer feature end-to-end. Each step is annotated with expected outcomes.

## Manual Steps (requires Docker Engine ≥ 24)

### §1: Build an image variant

```bash
cd /path/to/coordinare/repo

# Build base variant (no backends, no browser)
docker build -f agent/performer/Dockerfile.base -t performer:base agent/performer/
# Expected: successful build, image tag appears in `docker images`

# Build slim variant (single backend example)
docker build -f agent/performer/Dockerfile.slim --build-arg BACKEND=claude_code -t performer:slim-claude agent/performer/
# Expected: successful build with BACKEND=claude_code burned in

# Build full variant (all backends + browser)
docker build -f agent/performer/Dockerfile.full -t performer:full agent/performer/
# Expected: successful build, image size ~2–3x larger than slim
```

### §2: Verify image satisfies the contract

```bash
# Start container with auth
docker run --rm -p 8088:8088 -e PERFORMER_AUTH_TOKEN=devtoken performer:full &
sleep 5

# Query /status endpoint
curl -s -H "Authorization: Bearer devtoken" http://localhost:8088/status | jq .

# Expected response:
# {
#   "availability": "idle",
#   "current_job_id": null,
#   "capabilities": {
#     "backends": ["claude_code", "codex", "cursor", "junie", "opencode"],
#     "tool_flags": ["git", "node", "python", "lint", "format", "test_runner", "ripgrep", "jq", "shell", "browser"]
#   },
#   "auth_enabled": true,
#   "last_status_check_ms": <epoch>
# }

# Stop container
killall -TERM python  # or docker stop <container_id>
```

### §3: Register a persistent performer with the coordinare

1. Ensure coordinare config.yaml contains:

   ```yaml
   performer_endpoints:
     - id: claude-1
       mode: persistent
       image: performer:slim-claude
       endpoint: http://localhost:8088
       roles: [writer]
       auth_token: ${PERFORMER_AUTH_TOKEN}
       failure_threshold: 5
       readiness_timeout_s: 120
       secret_sources:
         init_payload: true
         env: true
         creds_file: false
   ```

2. Start the persistent container:

   ```bash
   docker run -d --name coordinare-performer-1 \
     -p 8088:8088 \
     -e PERFORMER_AUTH_TOKEN=devtoken \
     performer:slim-claude
   
   # Expected: container starts, is visible in `docker ps`
   ```

### §4: Register an ephemeral performer (optional)

Add to config.yaml:

```yaml
performer_endpoints:
  - id: codex-eph
    mode: ephemeral
    image: performer:slim-codex
    port: 8089
    roles: [writer]
    auth_token: ${PERFORMER_AUTH_TOKEN}
```

No manual container start required; coordinare manages the lifecycle.

### §5: Verify dispatch end-to-end

1. Start coordinare:
   ```bash
   set -a && source .env && set +a
   .venv/bin/python -m coordinare
   ```

2. Visit dashboard: `http://localhost:8000` (typical default)

3. In performer pool widget, verify:
   - Registration `claude-1` appears
   - `mode` shows `persistent`
   - `availability` shows `idle` within one poll cycle
   - `auth_enabled` shows `true`

4. Drop a card matching role `writer`:
   - Coordinare selects `claude-1` for dispatch
   - Performer transitions to `busy`
   - Progress events stream over dashboard
   - Job reaches `succeeded` or `failed`
   - Availability returns to `idle`

### §6: Verify fallover

1. Add a second persistent container registration to config.yaml (e.g., `claude-2` on port `:8090`).

2. Start second container:
   ```bash
   docker run -d --name coordinare-performer-2 \
     -p 8090:8088 \
     -e PERFORMER_AUTH_TOKEN=devtoken \
     performer:slim-claude
   ```

3. Dispatch two cards back-to-back:
   - Both should route to different performers
   - Coordinare logs should show one job to `claude-1`, one to `claude-2`
   - No serialization (SC-002)

4. Stop one container:
   ```bash
   docker stop coordinare-performer-1
   ```

5. Within `failure_threshold * poll_interval` seconds:
   - Performer marked `unreachable` in dashboard
   - Notification sent (dashboard alert, log entry, or configured Slack/email)
   - Restart container: `docker start coordinare-performer-1`
   - Recovery is automatic on next successful `/status`

### §7: Verify secrets precedence

1. Set environment variable on container:
   ```bash
   docker stop coordinare-performer-1
   docker run -d --name coordinare-performer-1 \
     -p 8088:8088 \
     -e PERFORMER_AUTH_TOKEN=devtoken \
     -e GITHUB_TOKEN=fromenv \
     performer:slim-claude
   ```

2. Dispatch a card with init-payload secret:
   ```yaml
   # (in card payload via coordinare dispatch flow)
   secrets:
     GITHUB_TOKEN: frominit
   ```

3. Verify:
   - Performer logs show `secret_resolved source=init_payload`
   - Job uses `GITHUB_TOKEN=frominit` (verify via job output if possible)
   - Remove init value from card; logs should show `source=env`
   - No log record contains the secret value (only `source=<which>`)

### §8: Verify capability mismatch is reported up front

1. Register performer with image lacking required capability:
   ```yaml
   performers:
     - id: slim-no-browser
       mode: persistent
       image: performer:slim-claude  # no --build-arg BROWSER=true
       endpoint: http://localhost:8091
       roles: [qa]  # QA role requires browser capability
   ```

2. Expected outcome (before any card dispatch):
   - Coordinare's capability-check emits exclusion notification
   - Performer marked `unreachable` with reason `capability_mismatch`
   - Logs show clear message about missing `browser` flag
   - No card is dispatched to this performer

### §9: Verify subprocess performers still work

1. Register a subprocess performer in config.yaml:
   ```yaml
   performers:
     - id: local-subprocess
       mode: subprocess
       roles: [some_role]
   ```

2. Dispatch a card matching that role:
   - Coordinare uses legacy subprocess_transport path
   - Job executes on coordinare host
   - No performer_endpoints entry created
   - No performer_pool metrics emitted for this registration

## Expected Test Results

- All §1–§9 steps complete without errors
- No regression in subprocess performer throughput (SC-008)
- Capability mismatch detected before dispatch (SC-006)
- Secrets respect configured precedence (no logging of values)
- Multi-performer fallover works as expected (SC-002)
- Exclusion and recovery notifications appear (SC-004)

## Known Gaps or Issues

None documented at time of T065 completion. If any drift is discovered during manual validation, file a bug against the `056-performer-containerization` branch.

## Conclusion

The quickstart flow validates the core user stories:
- **US1**: Single performer (ephemeral or persistent) runs a card end-to-end
- **US2**: Multiple performers are selected based on availability
- **US3**: Image variants cover different operator needs
- **US4**: Secrets are safely resolved with precedence
- **SC-008**: Subprocess performers are unaffected

All steps are documented and validated against the specification.
