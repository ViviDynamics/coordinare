# Data Model: Deterministic Env-Cache Native-Library Activation

No persisted coordinare state and no new Python data models. The "entities" here are
filesystem artifacts the profile script reads and produces at container runtime.

## Filesystem entities

### Env cache mount (input, read-only)
- **Path**: `/devenv/<slug>/`
- **Mode**: read-only bind/volume mount.
- **Relevant contents**:
  - `activate.sh` — LLM-authored activation script (sourced after deterministic step).
  - `debs/` — directory of captured `.deb` packages (may be absent).
  - host-built toolchain dirs (e.g. `ruby-3.4.2/`, `rbenv/`, `node-v18.12.1/`).

### Captured deb set (input, read-only)
- **Path**: `/devenv/<slug>/debs/*.deb`
- **Represents**: OS packages providing shared objects (`*.so*`) the host-built toolchain
  links against but the consumer image lacks (e.g. `libyaml-0-2`).
- **Absence**: if `debs/` is missing or empty → no extraction, no `LD_LIBRARY_PATH` change
  for that cache (FR-008).

### Writable per-cache lib dir (output)
- **Path**: `${_DEVENV_LIB_BASE:-/var/lib/devenv}/<slug>/lib`
- **Mode**: writable (ephemeral container layer, outside the read-only mount).
- **Contents**: shared objects extracted from the captured debs (flattened or via the
  deb's `usr/lib/...` layout, whichever the extraction strategy publishes).
- **Lifecycle**: created once per cache per container; prepended to `LD_LIBRARY_PATH`.

### Extraction sentinel (output)
- **Path**: `${_DEVENV_LIB_BASE:-/var/lib/devenv}/<slug>/.extracted`
- **Represents**: extraction for this cache completed and published atomically.
- **Semantics**: present ⇒ skip `dpkg-deb`, only ensure lib dir on `LD_LIBRARY_PATH`.
  Created **after** the lib dir is fully populated (atomic publish), so a partial dir is
  never seen as complete (FR-003, FR-009).

### Environment variables (process state)
- **`LD_LIBRARY_PATH`**: prepended with each cache's writable lib dir. Existing value
  preserved (append-after).
- **`_DEVENV_SOURCED`**: existing re-entry guard sentinel; unchanged semantics (FR-006).
- **`_DEVENV_LIB_BASE`** (optional, test seam): overrides the writable base dir; defaults
  to the fixed image path when unset.

## Relationships / flow

```
/devenv/<slug>/debs/*.deb   --(dpkg-deb -x, once)-->   <lib_base>/<slug>/lib/*.so*
                                                            |
                                                            +--> prepended to LD_LIBRARY_PATH
                                                            +--> guarded by <lib_base>/<slug>/.extracted

(then, per existing loop)   .  /devenv/<slug>/activate.sh   # sourced AFTER, sees populated LD_LIBRARY_PATH
```

## Validation rules

- A `.deb` that fails to extract is skipped; remaining debs and caches still process (FR-005).
- An unwritable `_DEVENV_LIB_BASE` degrades gracefully: shell still starts, that cache
  simply lacks the extra `LD_LIBRARY_PATH` entry (SC-005).
- Re-sourcing within the same process tree is a no-op via `_DEVENV_SOURCED` (FR-006).
