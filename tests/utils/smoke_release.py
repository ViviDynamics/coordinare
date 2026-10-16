"""Offline CLI doubles for the Docker smoke release resolver."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def release_script_env(
    tmp_path: Path,
    releases: str,
    *,
    gh_code: int = 0,
    manifest_statuses: dict[str, str] | None = None,
    token_response: str = '{"token": "anonymous-test-token"}',
) -> dict[str, str]:
    gh = tmp_path / "gh"
    gh.write_text('''#!/bin/sh
test "$*" = 'release list --exclude-drafts --exclude-pre-releases --limit 5 --json tagName -q .[].tagName' || exit 64
printf '%s\\n' "$TEST_RELEASE"
exit "$TEST_GH_CODE"
''')
    gh.chmod(0o700)
    curl = tmp_path / "curl"
    curl.write_text(f"#!{sys.executable}\n" + '''
import json
import os
import sys

args = sys.argv[1:]
url = args[-1]
for variant in ("base", "full"):
    repository = "vividynamics/coordinare-performer-" + variant
    if url == "https://ghcr.io/token?scope=repository:" + repository + ":pull":
        assert not any("Authorization:" in arg for arg in args)
        if os.environ["TEST_TOKEN_RESPONSE"] == "transport-error":
            sys.exit(22)
        print(os.environ["TEST_TOKEN_RESPONSE"])
        sys.exit(0)
    prefix = "https://ghcr.io/v2/" + repository + "/manifests/"
    if url.startswith(prefix):
        assert "Authorization: Bearer anonymous-test-token" in args
        accept = next(arg for arg in args if arg.startswith("Accept:"))
        for media_type in (
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
        ):
            assert media_type in accept
        assert "--head" in args
        assert args[args.index("--output") + 1] == "/dev/null"
        assert args[args.index("--write-out") + 1] == "%{http_code}"
        status = json.loads(os.environ["TEST_MANIFEST_STATUSES"]).get(
            variant + ":" + url[len(prefix):], "200",
        )
        if status == "transport-error":
            sys.exit(28)
        print(status, end="")
        sys.exit(0)
raise AssertionError("Unexpected curl invocation: " + repr(args))
''')
    curl.chmod(0o700)
    return {
        **os.environ,
        "PATH": str(tmp_path) + ":" + os.environ["PATH"],
        "TEST_RELEASE": releases,
        "TEST_GH_CODE": str(gh_code),
        "TEST_MANIFEST_STATUSES": json.dumps(manifest_statuses or {}),
        "TEST_TOKEN_RESPONSE": token_response,
        "GITHUB_OUTPUT": str(tmp_path / "output"),
    }
