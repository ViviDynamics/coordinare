"""Spec 265: the clean-room verifier pod for a Kubernetes env bootstrap.

The bootstrap pod mounts the devenv claim read-write and runs the bootstrap agent, so nothing it leaves behind is
trusted. Verification and provenance walking run in separate sequential pods after the bootstrap pod is gone. Each
mounts the claim read-only at the consumer path, with no checkout of the agent's work, no credentials and no stdin.
The verify pod runs the daemon's rendered verify.sh against project files taken from the bootstrap's commit.

This module is pure: it builds the Pod manifest and the in-pod setup. Running the pod is the runtime's job
(``KubernetesRuntime.run_verifier``).
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

from coordinare_service_inference.cache_provenance import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_ENTRIES,
    MAX_RESULT_BYTES,
    MAX_SCRIPT_BYTES,
    RESULT_SENTINEL,
)

# The container name, and the label that marks a verifier pod for the operator and the orphan sweep.
VERIFIER_CONTAINER = "verifier"
VERIFIER_ROLE_LABEL = "coordinare.role"
VERIFIER_ROLE = "env-verifier"
OWNER_LABEL = "coordinare.vividynamics.com/owner"
# A verifier that runs past this is a failed verification, and its pod is removed.
VERIFIER_DEADLINE_S = 900
VERIFIER_CPU_LIMIT = "2"
VERIFIER_MEMORY_LIMIT = "4Gi"
VERIFIER_POLL_INTERVAL_S = 2.0
VERIFIER_LOG_TAIL_CHARS = 2000
WALK_DIAGNOSTIC_PREFIX = "COORDINARE_WALK_DIAGNOSTIC="
# The result bound covers JSON only. Reserve its sentinel and newline inside
# the extra log allowance before allocating any bytes to diagnostics.
WALK_DIAGNOSTIC_MAX_BYTES = VERIFIER_LOG_TAIL_CHARS - len(RESULT_SENTINEL.encode("utf-8")) - len("\n")
VERIFIER_LOG_TAIL_LINES = 32
VERIFIER_LOG_MAX_BYTES = MAX_RESULT_BYTES + VERIFIER_LOG_TAIL_CHARS
VERIFIER_REMOVAL_TIMEOUT_S = 10
VERIFY_TEXT_MAX_BYTES = MAX_SCRIPT_BYTES
# Below the API/etcd object limit, including JSON and base64 expansion.
VERIFIER_POD_MAX_BYTES = 900 * 1024
# Claim files must be world readable and directories world traversable (o+rX).
VERIFIER_UID = 10001
VERIFIER_GID = 10001
VERIFIER_HOME = "/tmp"
_SCRATCH_VOLUME = "verifier-scratch"
# The project files the Rails smoke and the bundle need, fetched at the bootstrap's SHA. A path that is not listed is not
# passed in, so the verifier sees only what the commit declares.
PROJECT_FILE_PATHS: tuple[str, ...] = ("Gemfile", "Gemfile.lock", ".ruby-version", ".tool-versions")
# Bound serialized project JSON, including paths and escaping, before base64 expansion.
# Together with the verify text this leaves over 200 KiB for pod metadata and setup.
PROJECT_FILES_MAX_BYTES = 256 * 1024
_CACHE_VOLUME = "devenv-cache"
_POD_NAME_MAX = 63
_POD_NAME_UUID_CHARS = 8
_SAFE_RELATIVE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]*$")


class VerifierConfigurationError(ValueError):
    """Operator inputs prevent verification; retrying the same configuration cannot help."""


# The setup, run before the verify, with the verifier's own environment. It writes the daemon's verify.sh and the project
# files (from the commit) into a private directory, and resolves the bundle path the way bundler will see it. Bundler
# resolves a relative BUNDLE_PATH against the Gemfile's directory. A cache whose bundle was installed against a vendored
# Gemfile at the cache root therefore resolves against the cache root, not the checkout, so the path is made absolute here.
SETUP_PY = r'''
import base64, json, os, re, shlex, sys
d = sys.argv[1]
devenv = os.environ["DEVENV"]
files = json.loads(base64.b64decode(os.environ["PROJECT_FILES_B64"]).decode("utf-8"))
repo = os.path.join(d, "repo")
os.makedirs(repo, mode=0o700)
for rel, text in files.items():
    parts = rel.split("/")
    if not rel or rel.startswith("/") or any(p in ("", ".", "..") for p in parts):
        sys.exit("refused project path: " + repr(rel))
    target = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(text)
verify = os.path.join(d, "verify.sh")
with open(verify, "w", encoding="utf-8") as fh:
    fh.write(base64.b64decode(os.environ["VERIFY_B64"]).decode("utf-8"))
os.chmod(verify, 0o600)
bundle = None
cfg = os.path.join(devenv, ".bundle", "config")
if os.path.isfile(cfg):
    with open(cfg, encoding="utf-8") as fh:
        found = re.search(r'^BUNDLE_PATH:\s*"?([^"\n]+?)"?\s*$', fh.read(), re.M)
    if found:
        bundle = found.group(1)
        if not os.path.isabs(bundle):
            bundle = os.path.normpath(os.path.join(devenv, bundle))
if bundle is None and os.path.isdir(os.path.join(devenv, "project", "bundle")):
    bundle = os.path.join(devenv, "project", "bundle")
with open(os.path.join(d, "env.sh"), "w", encoding="utf-8") as fh:
    fh.write("export COORDINARE_REPO_DIR=" + shlex.quote(repo) + "\n")
    fh.write("export BUNDLE_GEMFILE=" + shlex.quote(os.path.join(repo, "Gemfile")) + "\n")
    if bundle is not None:
        fh.write("export BUNDLE_PATH=" + shlex.quote(bundle) + "\n")
'''

# Stdlib only, so an unavailable performer module can still be diagnosed. Read a
# bounded tail, discard an initial partial line, redact, then clip the encoded
# diagnostic. JSON escaping keeps even a multiline traceback on one log line.
WALK_DIAGNOSTICS_PY = r"""
from __future__ import annotations

import json, os, re, sys
prefix = os.environ["WALK_DIAGNOSTIC_PREFIX"]
limit = int(os.environ["WALK_DIAGNOSTIC_MAX_BYTES"])
with open(sys.argv[1], "rb") as fh:
    size = fh.seek(0, os.SEEK_END)
    offset = max(0, size - limit)
    fh.seek(offset)
    raw = fh.read(limit)
if offset:
    raw = raw.partition(b"\n")[2]
    if not raw:
        raw = b"stderr line exceeds diagnostic bound"
text = raw.decode("utf-8", "replace")
text = re.sub(r"(?:gh[pousr]_|github_pat_|sk-|AKIA)[A-Za-z0-9_./+=-]+", "[REDACTED]", text)
text = re.sub(r"(?i)bearer\s+\S+", "Bearer [REDACTED]", text)
text = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
text = re.sub(r'''(?i)((?:[\w-]*(?:token|password|secret|api[_-]?key)[\w-]*)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)''', r"\1[REDACTED]", text)
text = text.replace("COORDINARE_PROVENANCE=", "[PROVENANCE_MARKER]")
if text.strip():
    payload = json.dumps(text.strip(), ensure_ascii=True)
    budget = limit - len(prefix) - len("\n")
    print(prefix + payload[-budget:])
"""

# The container command. It runs with stdin closed, the verify's own environment, and the working directory set to the
# checkout the verifier built. Walk mode executes only trusted image code in its own pod.
# Both mounts are read only, so verification cannot change the content frozen by the later walk.
# The verify's exit code is the pod's result.
VERIFIER_SCRIPT = (
    "set -eu\n"
    "umask 077\n"
    'mkdir -p "$HOME"\n'
    # Kubernetes merges the streams. Emit one bounded diagnostic before the
    # captured walker result, leaving the sentinel on the final nonempty line.
    'if [ "$WALK_ONLY" = "1" ]; then\n'
    '  rc=0\n'
    '  python3 -P -m performer.cache_verifier </dev/null >"$HOME/walk.stdout" 2>"$HOME/walk.stderr" || rc=$?\n'
    '  python3 -c "$WALK_DIAGNOSTICS_PY" "$HOME/walk.stderr"\n'
    '  cat "$HOME/walk.stdout"\n'
    '  exit "$rc"\n'
    'fi\n'
    'd=$(mktemp -d)\n'
    'python3 -c "$SETUP_PY" "$d"\n'
    '. "$d/env.sh"\n'
    "export BASH_ENV= ENV= _DEVENV_SOURCED=1 _DEVENV_SKIP_SERVICES=1\n"
    'cd "$COORDINARE_REPO_DIR"\n'
    'bash -o pipefail "$d/verify.sh" </dev/null\n'
)


def verifier_pod_name(slug: str) -> str:
    """A DNS label for one verifier pod: the symphony slug plus a random suffix, within the 63 character bound."""
    cleaned = re.sub(r"[^a-z0-9-]", "-", slug.lower()).strip("-") or "symphony"
    suffix = uuid.uuid4().hex[:_POD_NAME_UUID_CHARS]
    head = cleaned[: _POD_NAME_MAX - len("coordinare-verify-") - _POD_NAME_UUID_CHARS - 1]
    return f"coordinare-verify-{head}-{suffix}".strip("-")


def check_project_files(files: dict[str, str]) -> None:
    """Refuse a project file whose path could leave the checkout, or whose total size is over the bound.

    Raises ``ValueError`` with the reason.
    """
    for rel in files:
        parts = rel.split("/")
        if not rel or rel.startswith("/") or not _SAFE_RELATIVE.match(rel) or any(p in ("", ".", "..") for p in parts):
            msg = f"project file path {rel!r} is not a plain relative path"
            raise ValueError(msg)
    total = len(json.dumps(files, ensure_ascii=False).encode("utf-8"))
    if total > PROJECT_FILES_MAX_BYTES:
        msg = f"project files total {total} bytes, over the {PROJECT_FILES_MAX_BYTES} byte bound"
        raise ValueError(msg)


@dataclass(frozen=True)
class VerifierSpec:
    """What one verifier pod verifies: the image, the claim and its subpath, the consumer mount path, the daemon's
    verify text, and the project files from the commit."""

    image: str
    cache_claim: str
    cache_subpath: str
    mount_path: str
    verify_text: str
    project_files: dict[str, str]
    activate_text: str | None = None
    walk_only: bool = False
    max_entries: int = DEFAULT_MAX_ENTRIES
    max_bytes: int = DEFAULT_MAX_BYTES


def configured_verifier_image(config: Any) -> str:
    """Resolve only daemon configuration, never a symphony's endpoint or overrides."""
    global_config = getattr(config, "global_config", config)
    image = getattr(global_config, "kubernetes_verifier_image", None)
    if image is None:
        image = getattr(global_config, "performer_image", None)
    return _validate_verifier_image(image)


def _validate_verifier_image(image: Any) -> str:
    if not isinstance(image, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", image) is None:
        message = "kubernetes_verifier_image requires a valid daemon image or performer_image"
        raise VerifierConfigurationError(message)
    return image


def build_verifier_pod_manifest(
    spec: VerifierSpec, *, name: str, namespace: str, image_pull_secrets: tuple[str, ...] = (), owner: str | None = None,
) -> dict[str, Any]:
    """The verifier Pod. Pure, so the spec can be checked without a cluster.

    The claim is mounted read-only at ``spec.mount_path`` (the path a consumer mounts), from ``spec.cache_subpath``. There
    is no checkout volume, no secret, no service-account token, and no stdin. The only inputs are the daemon's verify text
    and the project files from the commit, both passed as base64 environment values. The pod has a deadline and limits.
    """
    check_project_files(spec.project_files)
    verify_bytes = spec.verify_text.encode("utf-8")
    if len(verify_bytes) > VERIFY_TEXT_MAX_BYTES:
        message = f"verify text exceeds the {VERIFY_TEXT_MAX_BYTES} byte bound"
        raise ValueError(message)
    cache_claim, cache_subpath, mount_path, image = spec.cache_claim, spec.cache_subpath, spec.mount_path, spec.image
    _validate_verifier_image(image)
    env = {
        "DEVENV": mount_path,
        "HOME": VERIFIER_HOME,
        "VERIFY_B64": base64.b64encode(verify_bytes).decode("ascii"),
        "PROJECT_FILES_B64": base64.b64encode(json.dumps(spec.project_files, ensure_ascii=False).encode("utf-8")).decode("ascii"),
        "SETUP_PY": SETUP_PY,
        "WALK_ONLY": "1" if spec.walk_only else "0",
        "WALK_DIAGNOSTICS_PY": WALK_DIAGNOSTICS_PY,
        "WALK_DIAGNOSTIC_PREFIX": WALK_DIAGNOSTIC_PREFIX,
        "WALK_DIAGNOSTIC_MAX_BYTES": str(WALK_DIAGNOSTIC_MAX_BYTES),
        "PROVENANCE_CONFIG": json.dumps({"activate_sh_b64": base64.b64encode(spec.activate_text.encode("utf-8")).decode("ascii"),
                                        "max_entries": spec.max_entries,
                                        "max_bytes": spec.max_bytes}) if spec.walk_only and spec.activate_text is not None else "",
    }
    manifest: dict[str, Any] = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {"app.kubernetes.io/managed-by": "coordinare", VERIFIER_ROLE_LABEL: VERIFIER_ROLE},
        },
        "spec": {
            "restartPolicy": "Never",
            "activeDeadlineSeconds": VERIFIER_DEADLINE_S,
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            "imagePullSecrets": [{"name": secret} for secret in image_pull_secrets],
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": VERIFIER_UID,
                "runAsGroup": VERIFIER_GID,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [{
                "name": VERIFIER_CONTAINER,
                "image": image,
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                    "readOnlyRootFilesystem": True,
                },
                "stdin": False,
                "tty": False,
                "command": ["bash", "-c", VERIFIER_SCRIPT],
                "env": [{"name": k, "value": v} for k, v in env.items()],
                "volumeMounts": [{
                    "name": _CACHE_VOLUME,
                    "mountPath": mount_path,
                    "subPath": cache_subpath,
                    "readOnly": True,
                }, {"name": _SCRATCH_VOLUME, "mountPath": VERIFIER_HOME}],
                "resources": {"limits": {"cpu": VERIFIER_CPU_LIMIT, "memory": VERIFIER_MEMORY_LIMIT}},
            }],
            "volumes": [{
                "name": _CACHE_VOLUME,
                "persistentVolumeClaim": {"claimName": cache_claim, "readOnly": True},
            }, {"name": _SCRATCH_VOLUME, "emptyDir": {}}],
        },
    }

    if owner:
        manifest["metadata"]["labels"][OWNER_LABEL] = owner
    if len(json.dumps(manifest).encode("utf-8")) > VERIFIER_POD_MAX_BYTES:
        message = f"verifier pod exceeds the {VERIFIER_POD_MAX_BYTES} byte bound"
        raise ValueError(message)
    return manifest


__all__ = [
    "PROJECT_FILE_PATHS",
    "VERIFIER_DEADLINE_S",
    "VERIFIER_POLL_INTERVAL_S",
    "build_verifier_pod_manifest",
    "check_project_files",
    "verifier_pod_name",
]
