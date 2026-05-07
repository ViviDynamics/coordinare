---
name: restart-coordinare
description: Stop the running coordinare daemon, optionally rebuild the performer Docker image, then relaunch coordinare with env loaded from .env.
user-invocable: true
argument-hint: [--rebuild]
allowed-tools: Bash
effort: low
---

# Restart Coordinare

Stop the running coordinare daemon and relaunch it with the correct environment.

## Inputs

- `$ARGUMENTS` — Pass `--rebuild` to rebuild the performer Docker image before restarting.

## Current State

```!
pgrep -af "uv run.*coordinare" ; pgrep -af "python.*coordinare.__main__" ; pgrep -af "python -m coordinare" | grep -v grep || echo "NOT_RUNNING"
```

## Step 1: Check for running coordinare processes

Parse the output above. Coordinare may run as:
- A `uv run --env-file .env python -m coordinare` wrapper (PPID=1 when detached)
- A direct `.venv/bin/python -m coordinare` or `python -c "...coordinare.__main__..."` process

Kill **all** matching processes — both the `uv run` parent and the inner python child — so ports are released:

```bash
pkill -f "uv run.*coordinare" ; pkill -f "python.*coordinare.__main__" ; pkill -f "python -m coordinare"
```

Wait for them to exit and confirm ports 9090 and 9091 are free:

```bash
sleep 3 && netstat -anv 2>/dev/null | grep -E "9090|9091" | grep LISTEN || echo "ports free"
```

If ports are still LISTEN, run `pkill -9 -f "coordinare"` and wait another 3 seconds.
If ports are in TIME_WAIT (kernel hold after close), wait up to 30 more seconds for them to clear before launching.

If `NOT_RUNNING` and ports are already free, skip to Step 3.

## Step 2: Tell the user the old process has been stopped.

## Step 3: Rebuild performer image (only if --rebuild was passed)

If `$ARGUMENTS` contains `--rebuild`:

Build the base image first (Dockerfile.full inherits FROM coordinare-performer:base), then the full image.
The build context must be `agent/performer/` (not `.`) so that `COPY pyproject.toml` and `COPY src/` resolve to the performer package, not the coordinare root.

```bash
docker build -t coordinare-performer:base -f agent/performer/Dockerfile.base agent/performer/ 2>&1 | tail -8
docker build -t coordinare-performer:full -f agent/performer/Dockerfile.full agent/performer/ 2>&1 | tail -8
```

Report success or failure of each build. If either fails, stop and tell the user — do not start coordinare against a broken image.

## Step 4: Verify .env exists

```bash
test -f .env && echo "OK" || echo "MISSING"
```

If `.env` is missing, stop and tell the user to create it first.

## Step 5: Launch coordinare in the background

Use the `.venv` Python (not the system python) so all dependencies are available:

```bash
set -a && source .env && set +a && nohup .venv/bin/python -m coordinare --config config.yaml > /tmp/coordinare.log 2>&1 &
echo "PID=$!"
```

Capture the PID from the output.

## Step 6: Confirm it started

Wait 5 seconds, then check the health endpoint:

```bash
sleep 5 && curl -sf http://localhost:9090/health 2>/dev/null || tail -30 /tmp/coordinare.log
```

- If the health endpoint returns JSON: coordinare is running — report the status and PID to the user.
- If the curl fails: the process likely crashed. Show the last 40 lines of `/tmp/coordinare.log` and tell the user.

Also check the process is still alive:

```bash
pgrep -af "coordinare" | grep -v grep
```

## Important Guidelines

- Always source `.env` before launching — config.yaml uses `${VAR}` placeholders that expand at load time and silently become empty strings without it.
- Use `.venv/bin/python`, never the system `python` — the system python lacks project dependencies.
- Kill both the `uv run` wrapper and the inner python process; killing only the inner process lets `uv run` respawn it.
- Port TIME_WAIT after a kill is normal — wait it out, do not force-bind.
- Log output goes to `/tmp/coordinare.log`; the user can follow it with `tail -f /tmp/coordinare.log`.