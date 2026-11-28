# Sandboxed stage jobs leave the GitHub token readable by pull request code

Issue #450

## Scope
In:
- `sandbox-entrypoint.sh` starts dockerd (and so containerd) and the egress proxy heartbeat
  from an empty environment with only `PATH` and the proxy variables.
- The QAre stage job holds the token only for the checkout and GitHub's compare diff, then
  starts itself again (`execve`) without it before QAre plans, so the process that waits on
  QAre (while the pull request's code runs as root beside it) never held it: not in its
  environment, its arguments or its memory.
- An audit of the rest of the path (below), with tests for what it found.
- Docs: what the pull request's code can read.

Out:
- The values `env_passthrough` names (a model key) stay readable by the pull request's code,
  because QAre needs them while it runs that code. Moving the key out of the container (a
  key-injecting gateway, as the egress proxy is for egress) is a design change of its own,
  filed as a follow-up.
- Host-side `docker inspect` showing the env-file token: anyone with the host's Docker is
  already trusted (the issue says so).
- The live proof on a Linux host with Sysbox: no Docker here. The verification commands go
  on #450.

## Audit
- dockerd, containerd: inherited the container's environment (the leak). Fixed.
- Proxy heartbeat: a subshell, so it kept the shell's initial environment, and its `sleep`
  and interpreter children got the exported token. Fixed (a shell of its own, `env -i`).
- `docker info` and the first proxy wait in the entry point: run with the full environment,
  but end before the job starts. Left as they are.
- The stage job's own process: held the token in memory for the whole run (and before #423,
  in its environment). Fixed by the second restart.
- QAre and the checks it runs: never given the token (`qare_env`); unchanged.
- Egress proxy container: started with no `-e` and no env file. Covered by a test.
- Health probes: no token; the version probe gets the passthrough names, runs only the
  image's `qare --version`, and no pull request code. Unchanged.
- Non-sandbox QAre and Progettare containers (#432): `entrypoint.sh` leaves no process
  running beside the job, and the job restart above covers QAre. Progettare needs the token
  for its whole run (it reads the issue with `gh`) and runs no pull request code (a clone of
  the default branch); unchanged.

## Tasks
- [x] 1. Failing test: no process the sandbox entry point leaves running inherits a secret
  (name or value), with and without the egress proxy; the job still gets its token.
- [x] 2. Fix the entry point (`clean_env` for dockerd, `env -i` shell for the heartbeat).
- [x] 3. Failing test: the QAre job hands over after the checkout and diff, before QAre
  plans, with no secret in what it hands over; the run still reaches its verdict through the
  real entry; on Linux, the process that waits on QAre has no copy of the token in
  `/proc/<pid>/environ`, `cmdline` or `mem`.
- [x] 4. Split the run into `prepare` and `finish`, add `resume_job`, and restart from
  `__main__`.
- [x] 5. Test: the egress proxy container is given no variable.
- [x] 6. Docs.
