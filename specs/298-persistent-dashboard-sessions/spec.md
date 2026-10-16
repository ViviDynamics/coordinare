# Persistent dashboard login sessions

Dashboard OIDC sessions must remain valid across daemon restarts until their
original expiry. Store the session table in `dashboard_oidc_sessions.json` beside
the expanded, resolved `state_file_path`. Kubernetes deployments retain this file
on the state PVC.

Cookie values are random bearer credentials. Keep only their SHA256 hashes as
table keys, both in memory and on disk. Persist the verified identity, including
the server side provider logout hint, and absolute expiry in a versioned JSON
snapshot. Keep the existing cookie flags and session capacity.

Write changes synchronously on login, logout, capacity eviction, and observed
expiry. Successful lookups and unknown logout values perform no writes. Prune
expired entries before capacity eviction. At startup validate the entire table,
discard expired entries, retain the newest entries within capacity, and persist
any pruning. Missing files start empty. Corrupt or unreadable files start empty
with a warning containing only the exception type. Cleanup write failures never
prevent startup.

Use a temporary file in the same directory, set permissions to `0600` before
writing, flush and fsync, then atomically replace the destination. Every
replacement enforces `0600`, regardless of previous permissions. Clean up
temporary files on failure. Mutation write errors log a warning and propagate
rather than acknowledging durability; failed logout restores the in memory
entry so the removal can be retried. A single daemon owns this file; concurrent
replicas sharing a table are outside this change.

Tests are written before implementation and cover restart identity and expiry,
logout across restart, startup and lookup expiry cleanup, capacity across
restart, expired entry pruning, corrupt and unreadable files, private temporary
and final permissions, atomic replacement failure, logout retry, cookie hashing,
write frequency, daemon state path wiring, and the dashboard login and logout
routes without opening network sockets.

Existing sessions are still lost once, on the first deploy of this change.
Subsequent restarts preserve sessions stored on the state volume.
