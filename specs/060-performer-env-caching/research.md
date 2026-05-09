# Research: Performer Environment Caching (060)

## 1. GitHub Blob SHA for Change Detection

**Decision**: Use GitHub GraphQL API to fetch the blob `oid` (Git object ID / SHA1) for the target file on each poll cycle. Add a `get_file_blob_sha` method to `GitHubClient` using a new `GET_FILE_BLOB_SHA_QUERY`.

**Rationale**: The existing `GET_FILE_CONTENT_QUERY` already uses the `object(expression: "ref:path")` GraphQL pattern. Adding `oid` to the `... on Blob` fragment costs nothing extra (single query, no additional API call). The blob `oid` is the SHA1 hash of the file's content — it is stable: identical file content across different commits produces the same blob SHA, and any edit produces a different SHA. This gives us change detection with no content download cost on cycles where the file has not changed.

**Alternatives considered**:
- GitHub REST API `GET /repos/{owner}/{repo}/contents/{path}` — returns `sha` (blob SHA) + `content` (base64). Usable, but returns the full content (wasted bandwidth) unless we parse only the `sha` field. REST also uses per-user rate limits shared with other calls; GraphQL is cheaper per operation.
- GitHub REST `GET /repos/{owner}/{repo}/git/trees/{ref}` with `?recursive=1` — returns all tree entries with blob SHAs. Efficient for bulk checks but over-fetches when we only care about one file.
- Polling `git log --oneline -1 -- README.md` via subprocess on a local clone — requires maintaining a local clone per symphony, which is out of scope.

**New query** (lives in `services/github.py` alongside `GET_FILE_CONTENT_QUERY`):

```graphql
query GetFileBlobSha($owner: String!, $repo: String!, $expression: String!) {
  repository(owner: $owner, name: $repo) {
    object(expression: $expression) {
      ... on Blob {
        oid
      }
    }
  }
}
```

**Rate-limit cost**: One GraphQL query per symphony per poll cycle. At 30-second poll intervals and 10 symphonies, that is 20 queries/minute — well within GitHub's 5,000 points/hour budget.

---

## 2. Host-Path Volume Management for Docker Env Caches

**Decision**: Store each symphony's env cache at `{env_cache_root}/{sanitised_symphony_name}/`. Coordinare creates the directory (including parents) before the first `docker run`. Mount it at `/devenv` inside the container — read-write for `env_bootstrap`, read-only for all other performers.

**Rationale**:
- Coordinare already uses host-path `VolumeMount` (see `models/performer_endpoint.py`). Dynamic injection at dispatch time is the minimal extension: derive the `VolumeMount` from symphony config + cache state rather than requiring it in static performer config.
- `/devenv` is a clean, short, convention-friendly mount point. Tools installed there are accessed via `PATH`, `PYTHONPATH`, or tool-specific env vars set up by the bootstrap performer image — all of which are performer-image concerns.
- Read-only mounts for regular performers prevent accidental writes and make the volume safe to share across concurrent performers of the same symphony.
- The host-path approach (vs. named Docker volumes) keeps the cache directly inspectable by operators (`ls ~/.coordinare/env-caches/`) and avoids Docker volume lifecycle management.

**Directory creation**: `os.makedirs(path, exist_ok=True)` immediately before `docker run`. Idempotent and safe on restarts.

**Symphony name sanitisation** (for filesystem path safety):
```python
import re, hashlib

def sanitise_symphony_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9_-]", "-", name.lower()).strip("-")
    if not slug:
        slug = "symphony"
    suffix = hashlib.sha1(name.encode()).hexdigest()[:6]
    return f"{slug}-{suffix}"
```
The 6-char SHA1 suffix is always appended, not only on collision. Two names that produce the same slug base (e.g. `"Foo Bar"` and `"foo-bar"`) still get distinct suffixes because their SHA1s differ — making every slug globally unique by construction.

**Alternatives considered**:
- Named Docker volumes (`docker volume create`) — opaque to operators, requires Docker volume lifecycle management, harder to back up or inspect.
- tmpfs — ephemeral, defeats the purpose of caching.
- Bind-mount from within a container (Docker-in-Docker) — not viable; coordinare runs on the host, not in a container.
- S3 or remote volume backends — out of scope for this feature; noted in spec's Out of Scope section.

---

## 3. Bootstrap Serialisation (one per symphony at a time)

**Decision**: Track `env_bootstrap_in_flight: dict[str, bool]` in `EnvCacheState` (in-memory, per-symphony). Before dispatching a bootstrap, check this flag. If true, defer (do not dispatch). The next poll cycle with the README SHA still differing will re-evaluate and dispatch once the flag clears.

**Rationale**: Concurrent bootstraps for the same symphony would race on the shared volume, potentially leaving the env in a partial state. Serial execution is the safest default and matches the existing single-performer-per-card dispatch model.

**Alternatives considered**:
- File lock on the cache directory — more robust across process restarts but adds complexity (lock file cleanup on crash). Out of scope for initial implementation.
- Fencing with a sentinel file written by the performer — requires performer image cooperation; adds coupling.

---

## 4. Startup Behaviour (CoordinareState Reset)

**Decision**: On coordinare start, seed `env_readme_sha` by fetching the current blob SHA for each symphony that has `env_bootstrap` configured. If the fetched SHA differs from any previously persisted SHA, trigger a bootstrap. Since `CoordinareState` is in-memory and resets on restart, coordinare treats restart as "no prior SHA known" and always re-fetches; the comparison baseline is populated on the first pre-flight cycle.

**Rationale**: The existing pattern for `last_known_main_sha` (in `daemon.py`) seeds the SHA on first successful fetch and compares against it on subsequent cycles. We follow the same pattern.

**Alternatives considered**:
- Persist SHA to disk (e.g., a `.env-sha` file in each cache directory) — more durable across restarts, avoids a bootstrap on every restart even if README is unchanged. Considered a future enhancement; not required for initial implementation because restarts are infrequent and a bootstrap run is idempotent.
