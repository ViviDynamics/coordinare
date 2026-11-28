# Issue 376 acceptance launchers

Copies of the attended acceptance launchers and operator tools, so they are tracked.
They run from the gitignored `.agents/state`, which holds every run directory and proof. Each
proof (`.agents/state/376-local-validation-<sha>.json`) pins the sha256 of the copy it was run
with, so the copies here are a record and are not run in place.

- `launch-nonrails.py`: nare and Dettmore (non-Rails lane). Example:
  `uv run python .agents/state/376-launch-nonrails.py nare nare-native-0NN --base sha256:<pin> --head <sha>`
- `launch-rails.py`: Elm (Rails lane). It still reads `353-snapshots` from a deleted checkout, and
  must be repointed before Elm can run.
- `route.json`: the model route the launchers read (no secret; the key comes from the
  infrastructure `.env` at launch).
- `tools/`: `approval_watch.py` (waits for a probe or execution approval), `approve_probe.py` and
  `approve_exec.py` (identity-bound approvals after review), and `diff_gens.py` (diff two generations).

The base image pin, its rebuild recipe and its tar backup are recorded in the progress ledger.
