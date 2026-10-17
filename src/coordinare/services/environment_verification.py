"""Source material for clean verification, pinned and checked without a daemon clone."""
from __future__ import annotations

import base64
import hashlib
import json
import shlex
from typing import TYPE_CHECKING, Any

from coordinare.services.environment_acceptance import accepted_environment
from coordinare.services.kubernetes_verifier import check_project_files, credential_environment

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from coordinare.models.env_cache import EnvCacheState


async def verification_sources(state: Mapping[str, Any], name: str, cache: EnvCacheState) -> dict[str, str]:
    """Fetch only accepted source inputs at the accepted commit; reject any drift."""
    manifest = accepted_environment(cache)
    if manifest is None:
        message = "environment has no independently accepted contract; rebuild the cache"
        raise ValueError(message)
    github = (state.get("symphony_github_services") or {}).get(name) or state.get("github_service")
    config = state.get("coordinare_config")
    global_config = getattr(config, "global_config", None) or state.get("config")
    effective = global_config
    sym_config = (state.get("symphony_configs") or {}).get(name)
    if sym_config is not None:
        effective = sym_config.effective_config(global_config)
    for symphony in getattr(config, "symphonies", []):
        if symphony.name == name:
            effective = symphony.effective_config(global_config)
            break
    if github is None or effective is None:
        message = "clean verification cannot resolve repository source inputs"
        raise ValueError(message)
    files: dict[str, str] = {}
    for path, digest in manifest.inputs.items():
        text = await github.get_file_content(
            effective.github_org, effective.project_name or name, path, ref=manifest.source_commit,
        )
        if text is None or hashlib.sha256(text.encode()).hexdigest() != digest:
            message = "clean verification source inputs differ from the accepted snapshot"
            raise ValueError(message)
        files[path] = text
    check_project_files(files)
    return files


def source_setup(files: dict[str, str]) -> str:
    """Create private source files in the verifier, with no shell interpolation of their contents."""
    check_project_files(files)
    encoded = base64.b64encode(json.dumps(files, ensure_ascii=False).encode("utf-8")).decode("ascii")
    program = (
        "import base64,json,os,tempfile\n"
        "from pathlib import Path\n"
        "root=Path(tempfile.mkdtemp(prefix='coordinare-source-'))\n"
        f"files=json.loads(base64.b64decode({encoded!r}))\n"
        "for name,text in files.items():\n"
        " p=root/name\n"
        " p.parent.mkdir(parents=True,exist_ok=True)\n"
        " p.write_text(text,encoding='utf-8')\n"
        "print(root)\n"
    )
    return (
        "set -e\nCOORDINARE_REPO_DIR=$(/usr/local/bin/python -I <<'COORDINARE_SOURCE_PY'\n"
        + program + "COORDINARE_SOURCE_PY\n)\nexport COORDINARE_REPO_DIR\n"
    )


async def verification_environment(
    state: Mapping[str, Any], name: str, cache: EnvCacheState, performer_mounts: Sequence[Path],
) -> str:
    """Export bounded encoded credentials for restoration after trusted startup."""
    encoded = await verification_credentials(state, name, cache, performer_mounts)
    if not encoded:
        return ""
    return "".join("export " + key + "=" + shlex.quote(value) + "\n"
                   for key, value in credential_environment(encoded).items())


async def verification_credentials(
    state: Mapping[str, Any], name: str, cache: EnvCacheState, performer_mounts: Sequence[Path],
) -> str:
    """Resolve curated credentials just in time; values travel only in verifier stdin."""
    from coordinare.services.test_env_loader import TestEnvFileError, load_test_env, parse_test_env

    config = state.get("coordinare_config")
    global_config = getattr(config, "global_config", None) or state.get("config")
    symphony = (state.get("symphony_configs") or {}).get(name)
    if symphony is None:
        symphony = next((item for item in getattr(config, "symphonies", []) if item.name == name), None)
    test_env = getattr(symphony, "test_env", None)
    path = getattr(test_env, "repo_path", None) or cache.test_env_source
    values: dict[str, str] = {}
    if test_env is not None and test_env.host_path is not None:
        from pathlib import Path

        try:
            values = load_test_env(test_env, repo_root=Path("/"), performer_mounts=performer_mounts)
        except TestEnvFileError:
            message = "clean verifier test environment source is unavailable or refused"
            raise ValueError(message) from None
    elif path:
        from coordinare_service_inference.environment.models import source_path

        source_path(path)
        effective = symphony.effective_config(global_config) if symphony is not None else global_config
        github = (state.get("symphony_github_services") or {}).get(name) or state.get("github_service")
        if github is None or effective is None:
            message = "clean verifier cannot resolve configured test environment"
            raise ValueError(message)
        content = await github.get_file_content(
            effective.github_org, effective.project_name or name, path, ref=cache.bootstrap_commit_sha,
        )
        if content is None:
            message = "clean verifier test environment source is missing"
            raise ValueError(message)
        values = parse_test_env(content)
    if not values:
        return ""
    # The trusted interpreter has already started when these exports take effect.
    # Refuse startup hooks and internal control variables from external dotenv files.
    reserved = {"BASH_ENV", "ENV", "COORDINARE_ENV_SECRET_NAMES", "COORDINARE_REPO_DIR"}
    if any(key in reserved or key.startswith("COORDINARE_ENV_") for key in values):
        message = "test environment attempts to override verifier controls"
        raise ValueError(message)
    encoded = base64.b64encode(json.dumps(values).encode()).decode()
    credential_environment(encoded)  # Validate the same aggregate bound for both transports.
    return encoded
