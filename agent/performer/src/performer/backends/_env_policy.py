"""Shared subprocess-env merge policy for all agent backends (spec 088 B1).

Every backend launches its agent CLI with an env derived from the same four
layers: the container image env, the env-cache activation delta, the git
credential env, and the backend tool env. Before this module each backend
hand-rolled the merge and they diverged three ways:

- claude_code (post-#110) appended the cache PATH after the image PATH;
- openclaw/opencode/opencode_compat/codex/pi stripped the cache PATH entirely;
- hermes/junie passed the full cache PATH, putting the project's pinned old
  node ahead of the image's — the startup-crash class #110 fixed for
  claude_code.

The append-image-PATH-first policy is the standard because it is correct for
both shell models: agents that snapshot their launch env (Claude Code's Bash
tool) see the project toolchain, and agents that re-source activate.sh per
command (BASH_ENV) are unaffected since append is a superset of strip.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

# Used when the container image somehow has no PATH: a cache-only PATH would
# put the project's pinned old node first and crash a modern Node-based agent
# CLI at startup, which is exactly what the append policy exists to prevent.
SYSTEM_DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def build_subprocess_env(
    *,
    cache_env: Mapping[str, str],
    git_env: Mapping[str, str] | None = None,
    tool_env: Mapping[str, str] | None = None,
    extra: Mapping[str, str] | None = None,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Merge the agent-CLI launch env with the image-PATH-first append policy.

    Layers, later wins: ``base_env`` (default ``os.environ``) → ``cache_env``
    minus PATH → ``git_env`` → ``tool_env`` → ``extra``. The cache PATH is
    then appended after the image PATH with already-present dirs deduplicated,
    so the image's node resolves first while the env-cache toolchain
    (ruby/bundle/pyenv shims absent from the image) stays reachable.
    """
    base: Mapping[str, str] = os.environ if base_env is None else base_env

    cache_env_no_path = {k: v for k, v in cache_env.items() if k != "PATH"}
    env: dict[str, str] = {
        **base,
        **cache_env_no_path,
        **(git_env or {}),
        **(tool_env or {}),
        **(extra or {}),
    }

    cache_path = cache_env.get("PATH", "")
    if cache_path:
        image_path = base.get("PATH") or SYSTEM_DEFAULT_PATH
        seen = set(image_path.split(os.pathsep))
        appended: list[str] = []
        for d in cache_path.split(os.pathsep):
            if d and d not in seen:
                appended.append(d)
                seen.add(d)
        env["PATH"] = os.pathsep.join([image_path, *appended])

    return env
